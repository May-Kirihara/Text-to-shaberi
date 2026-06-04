"""StreamBridge: mic events → ws stream calls を直列に橋渡しする（loop スレッド専用）。"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from .audio.vad import StreamComplete, StreamStart, SubSegment

_STOP = object()


class StreamBridge:
    def __init__(
        self,
        ws: Any,
        *,
        on_log: Callable[[str], None] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        queue_max: int = 64,
    ) -> None:
        self.ws = ws
        self._on_log = on_log
        self._loop = loop
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._queue_max = queue_max
        self._input_open: bool = False
        self._ws_active: bool = False
        self._failed: bool = False
        self._mode: str | None = None
        self._fallback_buf: bytearray = bytearray()
        self._supported: bool = True
        self._closing: bool = False
        self._consumer: asyncio.Task[None] | None = None
        self._ws_idle: asyncio.Event = asyncio.Event()
        self._ws_idle.set()
        self._queue_overflow_warned: bool = False

    def set_supported(self, ok: bool) -> None:
        self._supported = ok

    def notify_ws_busy(self) -> None:
        self._ws_idle.clear()

    def notify_ws_idle(self) -> None:
        self._ws_active = False
        self._ws_idle.set()

    def start(self) -> None:
        if self._consumer is not None and not self._consumer.done():
            return
        self._closing = False
        lp = self._loop or asyncio.get_event_loop()
        self._consumer = lp.create_task(self._consume())

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    def handle_event(self, ev: object) -> None:
        if self._closing:
            return
        if self._queue.qsize() >= self._queue_max:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if not self._queue_overflow_warned:
                self._queue_overflow_warned = True
                self._log("[stream] キュー溢れで最古イベントを破棄")
        self._queue.put_nowait(ev)

    async def close(self) -> None:
        self._closing = True
        self._queue.put_nowait(_STOP)
        if self._consumer is not None:
            try:
                await asyncio.wait_for(self._consumer, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                self._consumer.cancel()
                if self._ws_active:
                    try:
                        await self.ws.cancel()
                    except Exception:
                        pass
        self._consumer = None
        self._ws_active = False
        self._input_open = False
        self._mode = None

    async def _consume(self) -> None:
        try:
            while True:
                ev = await self._queue.get()
                if ev is _STOP:
                    if self._ws_active:
                        try:
                            await self.ws.cancel()
                        except Exception:
                            pass
                    return
                if isinstance(ev, StreamStart):
                    if self._closing:
                        continue
                    self._input_open = True
                    self._failed = False
                    self._fallback_buf = bytearray()
                    if not self._supported:
                        self._mode = "fallback"
                        continue
                    try:
                        await asyncio.wait_for(self._ws_idle.wait(), timeout=8.0)
                    except asyncio.TimeoutError:
                        self._failed = True
                        self._log("[stream] 前発話の終了待ちタイムアウト→今回の発話を破棄")
                        continue
                    sid = await self.ws.start_stream(auto_tts=True)
                    if sid is None:
                        self._failed = True
                        self._log("[stream] start不可→今回の発話を破棄")
                        continue
                    self._ws_active = True
                    self._mode = "stream"
                elif isinstance(ev, SubSegment):
                    if self._failed:
                        continue
                    if self._mode == "fallback":
                        self._fallback_buf += ev.audio
                        continue
                    if self._ws_active:
                        await self.ws.send_stream_segment(ev.index, ev.audio)
                elif isinstance(ev, StreamComplete):
                    if self._mode == "fallback" and self._fallback_buf:
                        await self.ws.utter(bytes(self._fallback_buf), auto_tts=True)
                    elif self._ws_active:
                        await self.ws.end_stream()
                    self._input_open = False
                    self._mode = None
                    self._fallback_buf = bytearray()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"[stream] consumer 例外終了: {exc}")
