"""test_stream_bridge: StreamBridge の単体テスト。

pytest-asyncio 不使用。asyncio.run で完結。
"""
from __future__ import annotations

import asyncio

from text_to_shaberi.client.audio.vad import StreamComplete, StreamStart, SubSegment
from text_to_shaberi.client.stream_bridge import StreamBridge


class FakeWS:
    def __init__(self, start_result="sid", start_block=None, send_block=None):
        self.calls = []
        self._start_result = start_result
        self._start_block = start_block
        self._send_block = send_block

    async def start_stream(self, auto_tts=True):
        if self._start_block:
            await self._start_block.wait()
        self.calls.append(("start_stream",))
        return self._start_result

    async def send_stream_segment(self, index, pcm):
        if self._send_block:
            await self._send_block.wait()
        self.calls.append(("send", index, bytes(pcm)))

    async def end_stream(self):
        self.calls.append(("end",))

    async def utter(self, pcm, auto_tts=True):
        self.calls.append(("utter", bytes(pcm)))

    async def cancel(self):
        self.calls.append(("cancel",))


def _drain(t=0.05):
    return asyncio.sleep(t)


def test_normal_order():
    async def go():
        ws = FakeWS()
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(SubSegment(0, b"a"))
        br.handle_event(SubSegment(1, b"b"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ws.calls == [
            ("start_stream",),
            ("send", 0, b"a"),
            ("send", 1, b"b"),
            ("end",),
        ]
        await br.close()

    asyncio.run(go())


def test_unsupported_fallback():
    async def go():
        ws = FakeWS()
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.set_supported(False)
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(SubSegment(0, b"x"))
        br.handle_event(SubSegment(1, b"y"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("start_stream",) not in ws.calls
        assert ws.calls == [("utter", b"xy")]
        await br.close()

    asyncio.run(go())


def test_start_none_discard():
    async def go():
        ws = FakeWS(start_result=None)
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(SubSegment(0, b"z"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("start_stream",) in ws.calls
        send_calls = [c for c in ws.calls if c[0] == "send"]
        assert send_calls == []
        end_calls = [c for c in ws.calls if c[0] in ("end", "utter")]
        assert end_calls == []
        await br.close()

    asyncio.run(go())


def test_idle_gating():
    async def go():
        ws = FakeWS()
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.notify_ws_busy()
        br.handle_event(StreamStart())
        await _drain()
        assert ws.calls == []
        br.notify_ws_idle()
        await _drain(0.1)
        assert ("start_stream",) in ws.calls
        await br.close()

    asyncio.run(go())


def test_close_cancels_active_stream():
    async def go():
        ws = FakeWS()
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(SubSegment(0, b"a"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("end",) in ws.calls
        assert ("cancel",) not in ws.calls
        await br.close()
        assert ("cancel",) in ws.calls

    asyncio.run(go())


def test_close_no_cancel_after_idle():
    async def go():
        ws = FakeWS()
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        br.handle_event(SubSegment(0, b"a"))
        br.handle_event(StreamComplete())
        await _drain()
        br.notify_ws_idle()
        await _drain()
        await br.close()
        assert ("cancel",) not in ws.calls

    asyncio.run(go())


def test_close_during_start_await():
    async def go():
        block = asyncio.Event()
        ws = FakeWS(start_block=block)
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        br.handle_event(StreamStart())
        await _drain()
        assert ws.calls == []
        await asyncio.wait_for(br.close(), timeout=3.0)
        assert br._consumer is None
        block.set()

    asyncio.run(go())


def test_idempotent_start():
    async def go():
        ws = FakeWS()
        br = StreamBridge(ws, loop=asyncio.get_running_loop())
        br.start()
        t1 = br._consumer
        br.start()
        t2 = br._consumer
        assert t1 is t2
        await br.close()
        assert br._consumer is None
        br.start()
        assert br._consumer is not None
        br.handle_event(StreamStart())
        br.handle_event(SubSegment(0, b"r"))
        br.handle_event(StreamComplete())
        await _drain()
        assert ("start_stream",) in ws.calls
        await br.close()

    asyncio.run(go())


def test_queue_overflow():
    async def go():
        ws = FakeWS(start_block=asyncio.Event())
        logs = []
        br = StreamBridge(ws, on_log=logs.append, loop=asyncio.get_running_loop(), queue_max=2)
        br.start()
        br.handle_event(StreamStart())
        for i in range(20):
            br.handle_event(SubSegment(i, b"x"))
        for msg in logs:
            pass
        await br.close()

    asyncio.run(go())
