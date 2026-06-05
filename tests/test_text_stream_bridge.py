"""test_text_stream_bridge: TextStreamBridge の状態機械テスト。asyncio.run 完結。"""
from __future__ import annotations

import asyncio

from text_to_shaberi.client.audio.vad import StreamComplete, StreamStart
from text_to_shaberi.client.text_input import TextSegment
from text_to_shaberi.client.text_stream_bridge import TextStreamBridge


class FakeWS:
    def __init__(self, start_result="sid", speak_result=True, send_raises=False,
                 start_block=None, end_block=None, send_block=None, speak_block=None,
                 end_result=True):
        self.calls = []
        self._start_result = start_result
        self._speak_result = speak_result
        self._end_result = end_result
        self._send_raises = send_raises
        self._start_block = start_block
        self._end_block = end_block
        self._send_block = send_block
        self._speak_block = speak_block
        self.start_capture = None
        self.speak_capture = None

    async def start_text_stream(self, auto_tts=True, profile=None, capture=False):
        self.start_capture = capture
        if self._start_block is not None:
            await self._start_block.wait()
        self.calls.append(("start", profile))
        return self._start_result

    async def send_text_segment(self, index, text):
        if self._send_block is not None:
            await self._send_block.wait()
        if self._send_raises:
            raise RuntimeError("boom")
        self.calls.append(("send", index, text))

    async def end_text_stream(self):
        if self._end_block is not None:
            await self._end_block.wait()
        self.calls.append(("end",))
        return self._end_result

    async def speak(self, text, profile=None, capture=False):
        self.speak_capture = capture
        if self._speak_block is not None:
            await self._speak_block.wait()
        self.calls.append(("speak", text, profile))
        return self._speak_result

    async def cancel(self):
        self.calls.append(("cancel",))


def test_done_fired_when_start_fails():
    async def go():
        dones, errs = [], []
        ws = FakeWS(start_result=None)
        br = TextStreamBridge(
            ws, on_done=lambda: dones.append(1),
            on_error=lambda m: errs.append(m), loop=asyncio.get_running_loop(),
        )
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert errs  # on_error
        assert dones == [1]  # done は1回（reservation 解除を保証）
        await br.close()

    asyncio.run(go())


def test_done_fired_on_send_exception_and_consumer_survives():
    async def go():
        dones = []
        ws = FakeWS(send_raises=True)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))  # send 例外
        br.handle_event(StreamComplete())
        await _drain()
        assert dones == [1]  # 例外でも done
        # consumer は生存し、次 request も処理できる
        ws.calls.clear()
        br.handle_event(StreamStart())
        br.handle_event(StreamComplete())
        await _drain()
        assert ("start", None) in ws.calls
        await br.close()

    asyncio.run(go())


def test_abort_drops_pending_and_fires_done_immediately():
    # consumer 未処理の pending を abort で破棄。ws stream は始まらず即 done（codex#4）。
    async def go():
        dones = []
        ws = FakeWS()
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        br.handle_event(StreamComplete())
        br.abort()  # consumer がまだ動いていない（await していない）うちに abort
        await _drain()
        assert ("start", None) not in ws.calls  # pending 破棄→start されない
        assert dones == [1]
        await br.close()

    asyncio.run(go())


def test_abort_cancels_active_stream_and_done_on_idle():
    # 開始済み stream を abort: ws.cancel し、done は idle(stream_stopped 相当)後に1回。
    async def go():
        dones = []
        ws = FakeWS()
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        await _drain()
        assert ("start", None) in ws.calls
        br.abort()
        await _drain()
        assert ("cancel",) in ws.calls  # active stream は ws.cancel される
        assert dones == []  # まだ done を出さない（音声終了=idle 待ち）
        br.notify_ws_idle()  # stream_stopped → idle
        await _drain()
        assert dones == [1]  # idle で done（reservation 解除）
        await br.close()

    asyncio.run(go())


def test_abort_during_start_none_fires_done():
    # start_text_stream 待ち中に abort、かつ start が None を返しても done が出る（codex#5）。
    async def go():
        dones = []
        block = asyncio.Event()
        ws = FakeWS(start_result=None, start_block=block)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        await _drain()  # consumer は start_text_stream で block 待ち
        br.abort()      # busy・ws未開始 → gen++ / _abort_done_pending
        block.set()     # start が None を返す
        await _drain()
        assert dones == [1]  # 永久 busy にならず done
        await br.close()

    asyncio.run(go())


def test_abort_during_end_no_double_done():
    # end_text_stream 待ち中に abort→idle 先行で done が出た後、end が返っても二重 done しない（codex#5）。
    async def go():
        dones = []
        block = asyncio.Event()
        ws = FakeWS(end_block=block)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        await _drain()
        br.handle_event(StreamComplete())
        await _drain()  # consumer は end_text_stream で block 待ち
        br.abort()      # ws_active → cancel + _awaiting_done
        await _drain()
        br.notify_ws_idle()  # idle → done #1
        await _drain()
        block.set()     # end が True を返すが gen-mismatch で bail（done #2 を出さない）
        await _drain()
        assert dones == [1]  # ちょうど1回
        await br.close()

    asyncio.run(go())


def test_abort_during_idle_wait_timeout_fires_done():
    # idle 待ちが timeout しても abort 中なら done を出す（codex#6: 永久 busy 防止）。
    async def go():
        dones = []
        ws = FakeWS()
        br = TextStreamBridge(
            ws, on_done=lambda: dones.append(1),
            loop=asyncio.get_running_loop(), idle_timeout=0.05,
        )
        br.start()
        br.notify_ws_busy()  # _ws_idle clear → StreamStart の idle 待ちがブロック
        br.handle_event(StreamStart())
        await _drain(0.02)  # consumer は idle 待ち中
        br.abort()  # busy・ws未開始 → _abort_done_pending
        await _drain(0.12)  # idle 待ちが timeout → gen-check → fire done
        assert dones == [1]
        assert ("start", None) not in ws.calls
        await br.close()

    asyncio.run(go())


def test_abort_during_segment_send_exception_fires_done():
    # active stream の send 中に abort、その send が例外でも done が出る（codex#6: 永久 busy 防止）。
    async def go():
        dones = []
        block = asyncio.Event()
        ws = FakeWS(send_raises=True, send_block=block)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        await _drain()  # start ok, ws_active True
        br.handle_event(TextSegment(0, "a。"))
        await _drain()  # consumer は send_text_segment で block 待ち
        br.abort()  # ws_active → cancel + _awaiting_done=True
        await _drain()
        block.set()  # send が例外 → except は gen-mismatch で _awaiting_done を潰さない
        await _drain()
        assert dones == []  # まだ（idle 待ち）
        br.notify_ws_idle()  # idle → done
        await _drain()
        assert dones == [1]
        await br.close()

    asyncio.run(go())


def test_abort_during_fallback_speak_cancels_and_holds_done():
    # fallback speak が in-flight 中の abort: ws.cancel し、done は idle まで保持（codex#7: 早期解除で漏らさない）。
    async def go():
        dones = []
        block = asyncio.Event()
        ws = FakeWS(speak_block=block)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.set_supported(False)  # fallback
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "x。"))
        br.handle_event(StreamComplete())
        await _drain()  # consumer は speak() で block 待ち、_awaiting_done は既に True
        br.abort()  # in_flight(_awaiting_done) → cancel + hold
        await _drain()
        assert ("cancel",) in ws.calls  # ws.cancel される
        assert dones == []  # done は即時に出さない（音声終了=idle 待ち）
        block.set()
        await _drain()
        br.notify_ws_idle()  # idle → done
        await _drain()
        assert dones == [1]
        await br.close()

    asyncio.run(go())


def test_idle_before_end_returns_still_fires_done():
    # end_text_stream() return より先に WS idle が来ても done が出る（codex#7: check-then-set 競合）。
    async def go():
        dones = []
        block = asyncio.Event()
        ws = FakeWS(end_block=block)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        await _drain()
        br.handle_event(StreamComplete())
        await _drain()  # consumer は end() で block 待ち。_awaiting_done は await の前で True 済み
        br.notify_ws_idle()  # end() return より先に idle → done が出る
        await _drain()
        assert dones == [1]
        block.set()  # end() が True を返しても二重 done しない
        await _drain()
        assert dones == [1]
        await br.close()

    asyncio.run(go())


def test_idle_then_end_false_no_double_done():
    # end await 中に idle 先行(done #1)、その後 end が False を返しても二重 done しない（codex#8）。
    async def go():
        dones = []
        block = asyncio.Event()
        ws = FakeWS(end_block=block, end_result=False)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        await _drain()
        br.handle_event(StreamComplete())
        await _drain()  # consumer は end() で block 待ち、_awaiting_done は True
        br.notify_ws_idle()  # idle 先行 → done #1、_awaiting_done False
        await _drain()
        assert dones == [1]
        block.set()  # end が False を返す → fire_done にならず done #2 を出さない
        await _drain()
        assert dones == [1]
        await br.close()

    asyncio.run(go())


def test_idle_then_fallback_speak_false_no_double_done():
    # fallback speak await 中に idle 先行(done #1)、その後 speak が False を返しても二重 done しない。
    async def go():
        dones = []
        block = asyncio.Event()
        ws = FakeWS(speak_block=block, speak_result=False)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.set_supported(False)  # fallback
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "x。"))
        br.handle_event(StreamComplete())
        await _drain()  # consumer は speak() で block 待ち、_awaiting_done True
        br.notify_ws_idle()  # idle 先行 → done #1
        await _drain()
        assert dones == [1]
        block.set()  # speak が False を返す → 二重 done しない
        await _drain()
        assert dones == [1]
        await br.close()

    asyncio.run(go())


def test_done_fired_when_fallback_speak_not_started():
    async def go():
        dones = []
        ws = FakeWS(speak_result=False)  # speak 非開始(未接続/busy 相当)
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.set_supported(False)
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "x。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("speak", "x。", None) in ws.calls
        assert dones == [1]  # speak が始まらなくても done
        await br.close()

    asyncio.run(go())


def test_capture_passed_to_start_and_speak():
    async def go():
        ws = FakeWS()
        br = TextStreamBridge(ws, loop=asyncio.get_running_loop(), capture=True)
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ws.start_capture is True
        await br.close()

        ws2 = FakeWS()
        br2 = TextStreamBridge(ws2, loop=asyncio.get_running_loop(), capture=True)
        br2.set_supported(False)
        br2.start()
        br2.handle_event(StreamStart())
        br2.handle_event(TextSegment(0, "x。"))
        br2.handle_event(StreamComplete())
        await _drain()
        assert ws2.speak_capture is True
        await br2.close()

    asyncio.run(go())


def _drain(t=0.05):
    return asyncio.sleep(t)


def test_normal_order():
    async def go():
        ws = FakeWS()
        br = TextStreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        br.handle_event(TextSegment(1, "b。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ws.calls == [("start", None), ("send", 0, "a。"), ("send", 1, "b。"), ("end",)]
        await br.close()

    asyncio.run(go())


def test_unsupported_fallback():
    async def go():
        ws = FakeWS()
        br = TextStreamBridge(ws, loop=asyncio.get_running_loop())
        br.set_supported(False)
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "x。"))
        br.handle_event(TextSegment(1, "y。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("start", None) not in ws.calls
        assert ws.calls == [("speak", "x。y。", None)]
        await br.close()

    asyncio.run(go())


def test_profile_used_at_start():
    async def go():
        ws = FakeWS()
        br = TextStreamBridge(ws, loop=asyncio.get_running_loop())
        br.set_profile("p1")
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ws.calls[0] == ("start", "p1")
        await br.close()

    asyncio.run(go())


def test_start_none_discard():
    async def go():
        ws = FakeWS(start_result=None)
        errs = []
        br = TextStreamBridge(ws, on_error=lambda m: errs.append(m), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "z。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("start", None) in ws.calls
        assert [c for c in ws.calls if c[0] in ("send", "end", "speak")] == []
        assert errs
        await br.close()

    asyncio.run(go())


def test_idle_gating():
    async def go():
        ws = FakeWS()
        br = TextStreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.notify_ws_busy()
        br.handle_event(StreamStart())
        await _drain()
        assert ws.calls == []
        br.notify_ws_idle()
        await _drain(0.1)
        assert ("start", None) in ws.calls
        await br.close()

    asyncio.run(go())


def test_on_done_after_end_idle():
    async def go():
        dones = []
        ws = FakeWS()
        br = TextStreamBridge(ws, on_done=lambda: dones.append(1), loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(TextSegment(0, "a。"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("end",) in ws.calls
        br.notify_ws_idle()
        await _drain(0.02)
        assert dones == [1]
        await br.close()

    asyncio.run(go())


def test_control_events_not_dropped_on_overflow():
    async def go():
        errs = []
        ws = FakeWS()
        br = TextStreamBridge(
            ws, on_error=lambda m: errs.append(m),
            loop=asyncio.get_running_loop(), queue_max=2,
        )
        br.handle_event(StreamStart())
        for i in range(10):
            br.handle_event(TextSegment(i, "x。"))
        br.handle_event(StreamComplete())
        items = []
        while not br._queue.empty():
            items.append(br._queue.get_nowait())
        assert any(isinstance(x, StreamStart) for x in items)
        assert any(isinstance(x, StreamComplete) for x in items)
        assert errs

    asyncio.run(go())
