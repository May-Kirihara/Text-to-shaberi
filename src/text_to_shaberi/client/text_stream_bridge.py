"""TextStreamBridge: TextSegmenter events → ws text-stream calls を直列に橋渡し（loop専用）。

StreamBridge(音声) と同形の状態機械。差分:
- 呼ぶ ws メソッドが text 版（start_text_stream/send_text_segment/end_text_stream）。
- fallback は ws.speak(one-shot)。idle 待ち→done 相関(§14.6)。
- control event(StreamStart/StreamComplete) は overflow でも絶対に捨てない(§14.5)。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from .audio.vad import StreamComplete, StreamStart
from .text_input import TextSegment

_STOP = object()


class TextStreamBridge:
    def __init__(
        self,
        ws: Any,
        *,
        on_log: Callable[[str], None] | None = None,
        on_done: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        queue_max: int = 256,
    ) -> None:
        self.ws = ws
        self._on_log = on_log
        self._on_done = on_done
        self._on_error = on_error
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._queue_max = queue_max
        self._failed = False
        self._mode: str | None = None
        self._fallback: list[str] = []
        self._supported = True
        self._closing = False
        self._consumer: asyncio.Task | None = None
        self._ws_active = False
        self._ws_idle = asyncio.Event()
        self._ws_idle.set()
        self._awaiting_done = False
        self._profile: str | None = None

    def set_supported(self, ok: bool) -> None:
        self._supported = ok

    def set_profile(self, profile: str | None) -> None:
        self._profile = profile

    def notify_ws_busy(self) -> None:
        self._ws_idle.clear()

    def notify_ws_idle(self) -> None:
        self._ws_active = False
        self._ws_idle.set()
        if self._awaiting_done:
            self._awaiting_done = False
            if self._on_done:
                self._on_done()

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
        # control event(StreamStart/StreamComplete) は絶対に捨てない(§14.5)。
        # データ(TextSegment) のみ overflow 対象=ストリームを失敗にして error 通知。
        if isinstance(ev, TextSegment) and self._queue.qsize() >= self._queue_max:
            self._failed = True
            self._log("[text] 入力過多: このストリームを中断")
            if self._on_error:
                self._on_error("overflow")
            return
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
                    self._failed = False
                    self._fallback = []
                    if not self._supported:
                        self._mode = "fallback"
                        continue
                    try:
                        await asyncio.wait_for(self._ws_idle.wait(), timeout=8.0)
                    except asyncio.TimeoutError:
                        self._failed = True
                        self._log("[text] 前発話の終了待ちタイムアウト→破棄")
                        if self._on_error:
                            self._on_error("timeout")
                        continue
                    sid = await self.ws.start_text_stream(profile=self._profile)
                    if sid is None:
                        self._failed = True
                        self._log("[text] start不可→破棄")
                        if self._on_error:
                            self._on_error("start failed (busy/unsupported)")
                        continue
                    self._ws_active = True
                    self._mode = "stream"
                elif isinstance(ev, TextSegment):
                    if self._failed:
                        continue
                    if self._mode == "fallback":
                        self._fallback.append(ev.text)
                        continue
                    if self._ws_active:
                        await self.ws.send_text_segment(ev.index, ev.text)
                elif isinstance(ev, StreamComplete):
                    if self._mode == "fallback":
                        text = "".join(self._fallback)
                        if text.strip():
                            try:
                                await asyncio.wait_for(self._ws_idle.wait(), timeout=8.0)
                            except asyncio.TimeoutError:
                                pass
                            await self.ws.speak(text, self._profile)
                            self._awaiting_done = True
                        elif self._on_done:
                            self._on_done()
                    elif self._ws_active:
                        await self.ws.end_text_stream()
                        self._awaiting_done = True
                    elif self._failed and self._on_done:
                        self._on_done()
                    self._mode = None
                    self._fallback = []
                    self._profile = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"[text] consumer 例外終了: {exc}")
