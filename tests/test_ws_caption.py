"""ws_client の caption tee（「今喋ってる内容」を on_caption へ broadcast）のテスト。

speak/STT は音声同期（AudioBegin で表示・AudioEnd で消去）、text-stream は音声ゲート付き
蓄積（送信済みセグメントを append）を検証する。codex 指摘 NO-GO3（送信 await 中に
_finish_stream が走った時の次発話混入）と MED6（終了一回性）も網羅。

sync 経路は _on_message を直接駆動（非実行ループ共有）、async 経路（send_text_segment）は
asyncio.run + Fake _ws で1ループ内に閉じる。
"""
import asyncio

from text_to_shaberi.client.ws_client import WSClient
from text_to_shaberi.common.protocol import (
    AudioBegin,
    AudioEnd,
    AudioFormatPayload,
    ErrorMsg,
    Stopped,
    StreamStopped,
    Transcript,
)

# WSClient は loop.time() 等で loop を使う。同期テスト用に非実行ループを1つ共有。
_LOOP = asyncio.new_event_loop()
_FMT = AudioFormatPayload(codec="pcm_s16le", sample_rate=24000, channels=1)


class FakeSink:
    def __init__(self, **kw):
        self._muted = False

    def begin(self, fmt, device):
        pass

    def set_muted(self, muted):
        self._muted = muted

    def feed(self, pcm):
        return True

    def end(self):
        pass

    def abort(self):
        pass

    def join(self):
        pass


class FakeWS:
    """text-stream の送信先（_ws_send 用）。送信は no-op。"""

    async def send(self, data):
        pass


def _mk(on_caption, *, capture_id=None, stream_mode=False, auto_tts=False):
    ws = WSClient(
        "ws://127.0.0.1:1/ws",
        on_caption=on_caption,
        sink_factory=FakeSink,
        loop=_LOOP,
    )
    ws._active_id = "s1"
    ws._cancelling = False
    ws._stream_mode = stream_mode
    ws._stt_auto_tts = auto_tts
    if capture_id is not None:
        ws._capture_id = capture_id
    return ws


# ---- speak: 1発話＝1テキストを音声同期で表示・消去 ----

def test_speak_show_on_begin_clear_on_end():
    rec = []
    ws = _mk(lambda e, p: rec.append((e, p)))
    ws._caption_buf = ["こんにちは。"]
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    assert rec[0] == ("caption", {"text": "こんにちは。", "visible": True, "seq": 1})
    ws._on_binary(b"\x00\x00")  # チャンクでは字幕は変わらない
    ws._on_message(AudioEnd(id="s1"))
    assert rec[-1] == ("caption", {"text": "", "visible": False, "seq": 2})
    assert ws._active_id is None


# ---- STT: Transcript で text を確定し、AudioBegin で表示 ----

def test_stt_transcript_then_audio_synced():
    rec = []
    # auto_tts=True にしないと Transcript 受信で即 _finish_stream される（音声を待たない）。
    ws = _mk(lambda e, p: rec.append((e, p)), auto_tts=True)
    ws._on_message(Transcript(id="s1", text="やあ", final=True))
    assert rec == []  # まだ AudioBegin 前＝表示しない
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    assert rec[-1] == ("caption", {"text": "やあ", "visible": True, "seq": 1})
    ws._on_message(AudioEnd(id="s1"))
    assert rec[-1] == ("caption", {"text": "", "visible": False, "seq": 2})


# ---- text-stream: 音声ゲート付き蓄積（鳴り始めで表示→届くたび追記→鳴り終わりで消去） ----

def test_text_stream_accumulates_audio_gated():
    async def go():
        rec = []
        ws = WSClient(
            "ws://127.0.0.1:1/ws",
            on_caption=lambda e, p: rec.append((e, p)),
            sink_factory=FakeSink,
            loop=None,
        )
        ws._ws = FakeWS()
        ws._active_id = "s1"
        ws._stream_mode = True
        ws._cancelling = False
        ws._caption_buf = []
        # seg0 は AudioBegin 前＝バッファのみ（表示しない）
        await ws.send_text_segment(0, "あ")
        assert rec == []
        # 音が鳴り始めたら蓄積を表示
        ws._on_message(AudioBegin(id="s1", format=_FMT))
        assert rec[-1] == ("caption", {"text": "あ", "visible": True, "seq": 1})
        # 以降のセグメントは追記して伸ばす
        await ws.send_text_segment(1, "い")
        assert rec[-1] == ("caption", {"text": "あい", "visible": True, "seq": 2})
        await ws.send_text_segment(2, "う")
        assert rec[-1] == ("caption", {"text": "あいう", "visible": True, "seq": 3})
        # 鳴り終わりで消去
        ws._on_message(AudioEnd(id="s1"))
        assert rec[-1] == ("caption", {"text": "", "visible": False, "seq": 4})

    asyncio.run(go())


def test_text_stream_truncates_to_max_chars():
    async def go():
        rec = []
        ws = WSClient(
            "ws://127.0.0.1:1/ws",
            on_caption=lambda e, p: rec.append((e, p)),
            sink_factory=FakeSink,
            loop=None,
            caption_max=5,
        )
        ws._ws = FakeWS()
        ws._active_id = "s1"
        ws._stream_mode = True
        ws._cancelling = False
        ws._caption_buf = []
        ws._on_message(AudioBegin(id="s1", format=_FMT))  # 表示開始（空）
        await ws.send_text_segment(0, "あいう")
        await ws.send_text_segment(1, "えおかき")  # 合計7文字 → 末尾5文字に切詰め
        assert rec[-1][1]["text"] == "うえおかき"
        # buf も折りたたまれメモリ上限化されている
        assert ws._caption_buf == ["うえおかき"]

    asyncio.run(go())


def test_segment_after_finish_not_appended():
    """codex NO-GO3: 送信 await 中に _finish_stream が割り込んでも、終了後に
    古いセグメントを append して次発話へ混入させない。"""
    async def go():
        rec = []
        ws = WSClient(
            "ws://127.0.0.1:1/ws",
            on_caption=lambda e, p: rec.append((e, p)),
            sink_factory=FakeSink,
            loop=None,
        )

        class RacyWS:
            def __init__(self, ws):
                self.ws = ws

            async def send(self, data):
                # 送信中に発話終了が割り込む（AudioEnd 相当）
                self.ws._finish_stream()

        ws._ws = RacyWS(ws)
        ws._active_id = "s1"
        ws._stream_mode = True
        ws._cancelling = False
        ws._caption_open = True  # 表示中
        ws._caption_buf = []
        await ws.send_text_segment(0, "あ")
        # 送信中に _finish_stream → active_id None・buf クリア・caption_open False。
        # 送信後の append ガードで "あ" は混入しない。
        assert ws._caption_buf == []
        assert ("caption", {"text": "", "visible": False, "seq": 1}) in rec
        assert not any(p.get("text") == "あ" for _, p in rec)

    asyncio.run(go())


# ---- capture(forward_audio) 併発でも字幕は出す ----

def test_capture_still_emits_caption():
    rec = []
    ws = _mk(lambda e, p: rec.append((e, p)), capture_id="s1")
    ws._caption_buf = ["text"]
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    assert rec[0] == ("caption", {"text": "text", "visible": True, "seq": 1})
    ws._on_message(AudioEnd(id="s1"))
    assert rec[-1][1]["visible"] is False


# ---- 安全側（callback 無し・例外隔離） ----

def test_no_caption_callback_safe():
    ws = _mk(None)
    ws._caption_buf = ["x"]
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    ws._on_binary(b"\x00")
    ws._on_message(AudioEnd(id="s1"))
    assert ws._active_id is None  # 例外なく通過（emit は no-op）


def test_caption_emit_exception_swallowed():
    def boom(event, payload):
        raise RuntimeError("caption consumer exploded")

    ws = _mk(boom)
    ws._caption_buf = ["x"]
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    ws._on_message(AudioEnd(id="s1"))
    assert ws._active_id is None  # 例外が recv/再生へ漏れない


# ---- codex MED6: 終了一回性（begin 最大1回・clear ちょうど1回・begin無→clear無） ----

def test_termination_emits_exactly_one_clear():
    def term_audioend(ws):
        ws._on_message(AudioEnd(id="s1"))

    def term_stopped(ws):
        ws._on_message(Stopped(id="s1"))

    def term_streamstopped(ws):
        ws._on_message(StreamStopped(id="s1"))

    def term_error(ws):
        ws._on_message(ErrorMsg(id="s1", message="boom"))

    def term_finish_direct(ws):
        # disconnect(recv_loop finally) / cancel など全終端が funnel する経路
        ws._finish_stream()

    for term in (
        term_audioend,
        term_stopped,
        term_streamstopped,
        term_error,
        term_finish_direct,
    ):
        rec = []
        ws = _mk(lambda e, p: rec.append((e, p)))
        ws._caption_buf = ["X"]
        ws._on_message(AudioBegin(id="s1", format=_FMT))
        term(ws)
        shows = [p for _, p in rec if p["visible"]]
        clears = [p for _, p in rec if not p["visible"]]
        assert len(shows) == 1, term.__name__
        assert len(clears) == 1, term.__name__
        assert ws._active_id is None, term.__name__


def test_no_begin_no_clear():
    rec = []
    ws = _mk(lambda e, p: rec.append((e, p)))
    ws._caption_buf = ["X"]  # buf はあるが AudioBegin 未発火
    ws._on_message(AudioEnd(id="s1"))  # playback_id None の空stream終了
    assert rec == []  # begin 未発火なら clear も出さない
    assert ws._active_id is None


def test_format_invalid_no_caption():
    # AudioBegin の format 不正は caption open より前に _finish_stream するため
    # 表示も clear も出ない（begin 未発火扱い）。
    rec = []
    ws = _mk(lambda e, p: rec.append((e, p)))
    ws._caption_buf = ["X"]
    bad = AudioFormatPayload(codec="mp3", sample_rate=24000, channels=1)
    ws._on_message(AudioBegin(id="s1", format=bad))
    assert rec == []
    assert ws._active_id is None
