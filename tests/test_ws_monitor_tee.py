"""ws_client の monitor tee（全合成音声を on_monitor へ broadcast）のテスト。

_on_message(AudioBegin/AudioEnd) と _on_binary を直接駆動し、emit 3点
（begin/chunk/end）の整合・空ストリーム・capture 併発・例外隔離を検証する。
"""
import asyncio

from text_to_shaberi.client.ws_client import WSClient
from text_to_shaberi.common.protocol import AudioBegin, AudioEnd, AudioFormatPayload

# WSClient は loop.time() 等で loop を使う。同期テスト用に非実行ループを1つ共有。
_LOOP = asyncio.new_event_loop()


class FakeSink:
    def __init__(self, **kw):
        self.fed = []
        self._muted = False

    def begin(self, fmt, device):
        pass

    def set_muted(self, muted):
        self._muted = muted

    def feed(self, pcm):
        self.fed.append(pcm)
        return True

    def end(self):
        pass

    def abort(self):
        pass

    def join(self):
        pass


def _mk(on_monitor, *, on_audio=None, capture_id=None):
    ws = WSClient(
        "ws://127.0.0.1:1/ws",
        on_monitor=on_monitor,
        on_audio=on_audio,
        sink_factory=FakeSink,
        loop=_LOOP,
    )
    ws._active_id = "s1"
    ws._cancelling = False
    if capture_id is not None:
        ws._capture_id = capture_id
    return ws


def test_monitor_tee_normal_stream_begin_chunk_end():
    rec = []
    ws = _mk(lambda k, p: rec.append((k, p)))
    fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=24000, channels=1)
    ws._on_message(AudioBegin(id="s1", format=fmt))
    ws._on_binary(b"\x01\x02\x03\x04")
    ws._on_message(AudioEnd(id="s1"))
    assert [k for k, _ in rec] == ["begin", "chunk", "end"]
    assert rec[0][1] == {"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1}
    assert rec[1][1] == b"\x01\x02\x03\x04"
    assert rec[2][1] is None


def test_monitor_tee_empty_stream_no_begin_no_end():
    rec = []
    ws = _mk(lambda k, p: rec.append((k, p)))
    # 音声ゼロ完了（AudioBegin 無しで active を解放）→ begin/end は出ない。
    ws._on_message(AudioEnd(id="s1"))
    assert rec == []


def test_monitor_tee_capture_stream_also_fires():
    mon, cap = [], []
    ws = _mk(
        lambda k, p: mon.append((k, p)),
        on_audio=lambda k, p: cap.append((k, p)),
        capture_id="s1",
    )
    fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=16000, channels=1)
    ws._on_message(AudioBegin(id="s1", format=fmt))
    ws._on_binary(b"\xaa\xbb")
    ws._on_message(AudioEnd(id="s1"))
    # capture でも monitor は全イベント発火（ローカル再生抑制とは独立）。
    assert [k for k, _ in mon] == ["begin", "chunk", "end"]
    assert [k for k, _ in cap] == ["begin", "chunk", "end"]


def test_monitor_tee_double_begin_ignored():
    rec = []
    ws = _mk(lambda k, p: rec.append((k, p)))
    fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=24000, channels=1)
    ws._on_message(AudioBegin(id="s1", format=fmt))
    ws._on_message(AudioBegin(id="s1", format=fmt))  # 二重 begin は side-band 扱いで無視
    ws._on_binary(b"\x01\x02")
    ws._on_message(AudioEnd(id="s1"))
    # monitor へは begin が1回だけ（framing 保証）。
    assert [k for k, _ in rec] == ["begin", "chunk", "end"]


def test_muted_propagates_to_sink_and_feed_continues():
    # ミュートは sink/engine の出力側で無音化する設計。feed は継続し、monitor も継続。
    mon = []
    ws = _mk(lambda k, p: mon.append((k, p)))
    ws.set_muted(True)
    fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=24000, channels=1)
    ws._on_message(AudioBegin(id="s1", format=fmt))
    sink = ws._sink  # AudioBegin で生成された sink
    assert sink._muted is True  # ミュート状態が sink へ伝播
    ws._on_binary(b"\x01\x02")
    assert sink.fed == [b"\x01\x02"]  # feed は継続（無音化は出力側）
    ws._on_message(AudioEnd(id="s1"))
    assert [k for k, _ in mon] == ["begin", "chunk", "end"]  # monitor は継続


def test_unmuted_sink_not_muted_and_feeds():
    ws = _mk(lambda k, p: None)
    fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=24000, channels=1)
    ws._on_message(AudioBegin(id="s1", format=fmt))
    sink = ws._sink
    assert sink._muted is False
    ws._on_binary(b"\xab\xcd")
    assert sink.fed == [b"\xab\xcd"]
    ws._on_message(AudioEnd(id="s1"))


def test_set_muted_propagates_to_live_sink():
    # ストリーム中の set_muted は既存 sink へ即伝播する（mid-stream トグル）。
    ws = _mk(lambda k, p: None)
    fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=24000, channels=1)
    ws._on_message(AudioBegin(id="s1", format=fmt))  # sink 生成
    sink = ws._sink
    ws.set_muted(True)
    assert sink._muted is True  # live な sink へ即伝播
    ws.set_muted(False)
    assert sink._muted is False


def test_monitor_tee_exception_swallowed():
    def boom(kind, payload):
        raise RuntimeError("monitor consumer exploded")

    ws = _mk(boom)
    fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=24000, channels=1)
    # emit 例外が再生/recv へ漏れない（best-effort tee）。例外が伝播しないことを確認。
    ws._on_message(AudioBegin(id="s1", format=fmt))
    ws._on_binary(b"\x01\x02")
    ws._on_message(AudioEnd(id="s1"))
    # ここまで例外が出ずに到達できれば握れている。
    assert ws._active_id is None  # _finish_stream まで通った
