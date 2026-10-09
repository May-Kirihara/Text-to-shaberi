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


def _mk(on_caption, *, capture_id=None, stream_mode=False, auto_tts=False, **ws_kwargs):
    ws = WSClient(
        "ws://127.0.0.1:1/ws",
        on_caption=on_caption,
        sink_factory=FakeSink,
        loop=_LOOP,
        **ws_kwargs,
    )
    ws._active_id = "s1"
    ws._cancelling = False
    ws._stream_mode = stream_mode
    ws._stt_auto_tts = auto_tts
    if capture_id is not None:
        ws._capture_id = capture_id
    return ws


def _complete_playback(ws):
    """AudioEnd 正常パスは字幕消去を「実再生終了」まで遅延（loop.call_later）する。
    テストは非実行ループなのでタイマーが発火しない。予約された遅延消去を、実際に
    タイマーが呼ぶのと同じ pid で手動発火し『再生完了』を再現する。"""
    assert ws._caption_clear_timer is not None, "遅延消去が予約されていない"
    pid = ws._caption_pid
    ws._cancel_caption_clear_timer()
    ws._do_caption_clear(pid)


# ---- speak: 1発話＝1テキストを音声同期で表示・消去 ----

def test_speak_show_on_begin_clear_on_end():
    rec = []
    ws = _mk(lambda e, p: rec.append((e, p)))
    ws._caption_buf = ["こんにちは。"]
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    assert rec[0] == ("caption", {"text": "こんにちは。", "visible": True, "seq": 1})
    ws._on_binary(b"\x00\x00")  # チャンクでは字幕は変わらない
    ws._on_message(AudioEnd(id="s1"))
    # AudioEnd（データ受信完了）では即消去せず、実再生終了に同期して遅延消去する。
    assert ws._active_id is None
    assert ws._caption_open is True
    assert not any(not p["visible"] for _, p in rec)  # まだ clear は出ていない
    _complete_playback(ws)  # 実再生完了を再現 → ここで消去
    assert rec[-1] == ("caption", {"text": "", "visible": False, "seq": 2})
    assert ws._caption_open is False


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
    _complete_playback(ws)  # 消去は実再生終了に同期（遅延）
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
        # 鳴り終わり（データ受信完了）では遅延予約 → 実再生終了で消去
        ws._on_message(AudioEnd(id="s1"))
        assert not any(not p["visible"] for _, p in rec)  # まだ clear は出ていない
        _complete_playback(ws)
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
        # AudioEnd 正常終了は消去を遅延する。他終端（stopped/error/finish）は即消去。
        # 遅延中（caption_open のまま）なら実再生完了を再現して、最終的な clear 数を検証。
        if ws._caption_open:
            _complete_playback(ws)
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


# ---- 消去の「実再生終了」同期（遅延消去） ----

def test_caption_clear_delay_scales_with_audio_length():
    # 遅延秒数が受信した音声の実長に追従する（長い発話ほど遅く消える）。
    def delay_for(nbytes):
        ws = _mk(lambda e, p: None)
        ws._caption_buf = ["x"]
        ws._on_message(AudioBegin(id="s1", format=_FMT))  # 24000Hz mono s16le
        if nbytes:
            ws._on_binary(b"\x00" * nbytes)
        return ws._compute_caption_clear_delay()

    short = delay_for(24000 * 2 // 10)   # 0.1 秒分
    long = delay_for(24000 * 2 * 2)      # 2.0 秒分
    assert long - short > 1.5            # 音声長差(~1.9s)がほぼ delay 差に出る
    assert long > 2.0


# ---- ホールド（余韻）: 実再生終了後の dwell（dwell + replace） ----

def _hold_delay_for(text, **hold_kwargs):
    """buf=text・音声1.0秒固定で消去遅延を算出（ホールド値だけを動かす）。"""
    ws = _mk(lambda e, p: None, **hold_kwargs)
    ws._caption_buf = [text]
    ws._on_message(AudioBegin(id="s1", format=_FMT))  # 24000Hz mono s16le
    ws._on_binary(b"\x00" * (24000 * 2))              # 1.0 秒分
    return ws._compute_caption_clear_delay()


def test_caption_hold_scales_with_text_length():
    # 同じ音声長でも表示文字数が多いほどホールドが伸びる（長文の読み残し対策）。
    # 既定: 1字 hold=min(2000+20,5000)=2020ms / 100字 hold=min(2000+2000,5000)=4000ms
    short = _hold_delay_for("あ")
    long = _hold_delay_for("あ" * 100)
    assert long - short > 1.5           # 文字数差(99字×20ms ≈ 1.98s)がほぼ出る
    assert long > short


def test_caption_hold_capped_by_max():
    # base+文字数分が hold_max(5000ms) に達したらそれ以上伸びない。
    d200 = _hold_delay_for("あ" * 200)  # min(2000+4000,5000)=5000 → cap 到達
    d400 = _hold_delay_for("あ" * 400)  # min(2000+8000,5000)=5000
    assert abs(d400 - d200) < 0.1       # cap で打ち切り→差ほぼ0


def test_caption_hold_max_smaller_than_base():
    # hold_max_ms < hold_base_ms は上限が優先（min() 挙動・ドキュメント明記の動作）。
    d = _hold_delay_for("あ" * 10, caption_hold_base_ms=2000,
                        caption_hold_char_ms=20, caption_hold_max_ms=1000)
    # base(2000)+10字×20=2200ms だが max=1000 で打ち切り → hold=1000ms
    # 隣接確認: char分を伸ばしても max で同じ値になることを対比で保証
    d2 = _hold_delay_for("あ" * 100, caption_hold_base_ms=2000,
                         caption_hold_char_ms=20, caption_hold_max_ms=1000)
    assert abs(d2 - d) < 0.1
    # ホールド無効との差がちょうど max(1000ms)分＝cap 値そのものが上乗せされる
    off = _hold_delay_for("あ" * 10, caption_hold_base_ms=0,
                          caption_hold_char_ms=0, caption_hold_max_ms=0)
    assert 0.8 < d - off < 1.2


def test_caption_hold_disabled_matches_legacy_delay():
    # hold 3引数とも 0 → 従来動作（実再生終了+余白のみ・+2秒の余韻なし）。
    off = _hold_delay_for("あ" * 50, caption_hold_base_ms=0, caption_hold_char_ms=0,
                          caption_hold_max_ms=0)
    on = _hold_delay_for("あ" * 50)
    # 旧来値: prime(500)+preroll(200)+音声(1000)+margin(150) ≈ 1.85s
    assert abs(off - 1.85) < 0.2
    # 既定 hold（50字: min(2000+1000,5000)=3000ms）が上乗せされる
    assert on - off > 2.5


def test_deferred_clear_does_not_clear_next_caption():
    # A→B 連続発話: A の遅延消去（pid=A）が B の字幕を消さない（pid ガード＋begin で cancel）。
    rec = []
    ws = _mk(lambda e, p: rec.append((e, p)))
    ws._caption_buf = ["A"]
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    pid_a = ws._caption_pid
    ws._on_message(AudioEnd(id="s1"))               # A: 遅延消去を予約・active_id None
    assert ws._caption_open is True
    # 発話 B 開始（A のタイマーが残ったまま）。AudioBegin が A のタイマーを cancel。
    ws._active_id = "s2"
    ws._caption_buf = ["B"]
    ws._on_message(AudioBegin(id="s2", format=_FMT))
    assert rec[-1] == ("caption", {"text": "B", "visible": True, "seq": 2})
    # A の遅延消去が万一発火しても pid 不一致で no-op（B は消えない）。
    ws._do_caption_clear(pid_a)
    assert ws._caption_open is True
    assert rec[-1][1]["text"] == "B"


def test_clear_caption_now_clears_deferred():
    # disconnect(recv finally)/teardown/close が呼ぶ後始末で、遅延中の字幕＋タイマーを確実に始末。
    rec = []
    ws = _mk(lambda e, p: rec.append((e, p)))
    ws._caption_buf = ["hi"]
    ws._on_message(AudioBegin(id="s1", format=_FMT))
    ws._on_message(AudioEnd(id="s1"))
    assert ws._caption_open is True and ws._caption_clear_timer is not None
    ws._clear_caption_now()
    assert ws._caption_open is False
    assert ws._caption_pid is None
    assert ws._caption_clear_timer is None
    assert rec[-1] == ("caption", {"text": "", "visible": False, "seq": 2})


def test_cancel_clears_deferred_caption():
    # defer 中（AudioEnd 済み・active_id None・字幕表示中）でも cancel/stop で即消去できる。
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
        ws._cancelling = False
        ws._caption_buf = ["hi"]
        ws._on_message(AudioBegin(id="s1", format=_FMT))
        ws._on_message(AudioEnd(id="s1"))
        assert ws._caption_open is True and ws._active_id is None
        assert ws._caption_clear_timer is not None
        await ws.cancel()
        assert ws._caption_open is False
        assert ws._caption_clear_timer is None
        assert rec[-1] == ("caption", {"text": "", "visible": False, "seq": 2})

    asyncio.run(go())
