"""test_text_stream_bridge: TextStreamBridge の状態機械テスト。asyncio.run 完結。"""
from __future__ import annotations

import asyncio

from text_to_shaberi.client.audio.vad import StreamComplete, StreamStart
from text_to_shaberi.client.text_input import TextSegment
from text_to_shaberi.client.text_stream_bridge import TextStreamBridge


class FakeWS:
    def __init__(self, start_result="sid"):
        self.calls = []
        self._start_result = start_result

    async def start_text_stream(self, auto_tts=True, profile=None):
        self.calls.append(("start", profile))
        return self._start_result

    async def send_text_segment(self, index, text):
        self.calls.append(("send", index, text))

    async def end_text_stream(self):
        self.calls.append(("end",))

    async def speak(self, text, profile=None):
        self.calls.append(("speak", text, profile))

    async def cancel(self):
        self.calls.append(("cancel",))


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
