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
        capture: bool = False,
        idle_timeout: float = 8.0,
    ) -> None:
        self.ws = ws
        self._on_log = on_log
        self._on_done = on_done
        self._on_error = on_error
        self._loop = loop
        self._idle_timeout = idle_timeout
        self._queue: asyncio.Queue = asyncio.Queue()
        self._queue_max = queue_max
        self._failed = False
        self._overflow = False
        self._gen = 0  # abort 世代。consumer は await 跨ぎで gen 変化を見て bail する。
        self._busy = False  # consumer が _handle_event 実行中
        self._abort_done_pending = False  # abort が busy 中に来た→consumer 側で done を出す
        self._cancel_tasks: set = set()  # _safe_cancel_ws の未完了タスク保持（GC回避）
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
        self._capture = capture

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
            # overflow 専用フラグ（_failed と別管理で StreamStart の reset に消されない）。
            # on_error はストリーム毎に1回だけ（noisy 回避・codex LOW）。
            if not self._overflow:
                self._overflow = True
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
        # 残っている ws.cancel タスクを待つ（未観測タスク回避・codex LOW）。
        for t in list(self._cancel_tasks):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                t.cancel()
        self._cancel_tasks.clear()

    def _safe_cancel_ws(self) -> None:
        lp = self._loop or asyncio.get_event_loop()
        t = lp.create_task(self._safe_cancel())
        self._cancel_tasks.add(t)
        t.add_done_callback(self._cancel_tasks.discard)

    async def _safe_cancel(self) -> None:
        try:
            await self.ws.cancel()
        except Exception:
            pass

    def abort(self) -> None:
        # cancel 経路: pending イベントを破棄し、in-flight stream を止め、idle 後(=音声終了後)に
        # on_done で reservation を解除する。早期解除で late audio が新接続へ漏れるのを防ぐ(codex#4)。
        self._gen += 1
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._failed = False
        self._overflow = False
        self._mode = None
        self._fallback = []
        self._profile = None
        # in-flight 判定は _ws_active(stream) だけでなく _awaiting_done(fallback speak 含む)も見る。
        if self._ws_active or self._awaiting_done:
            self._awaiting_done = True  # ws idle(stream_stopped)後に on_done
            self._abort_done_pending = False
            self._safe_cancel_ws()
        elif self._busy:
            # consumer が start 途中の可能性。bail 時に done を出させる。
            self._abort_done_pending = True
        else:
            self._awaiting_done = False
            self._abort_done_pending = False
            if self._on_done:
                self._on_done()

    def _fire_abort_done(self) -> None:
        if self._abort_done_pending:
            self._abort_done_pending = False
            if self._on_done:
                self._on_done()

    async def _consume(self) -> None:
        # 1 request の終端では必ず on_done を1回出す（reservation を確実に解除し永久 busy を防ぐ）。
        # イベント処理中の例外でも consumer は死なず、当該 request を done で締める。
        try:
            while True:
                ev = await self._queue.get()
                if ev is _STOP:
                    if self._ws_active:
                        await self._safe_cancel()
                    return
                self._busy = True
                gen = self._gen
                try:
                    await self._handle_event(ev)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log(f"[text] イベント処理失敗: {exc}")
                    if self._gen != gen:
                        # abort が割り込んだ: 状態/done は abort(idle) 側が担う。ここでは潰さない。
                        pass
                    else:
                        self._failed = True
                        if self._on_error:
                            self._on_error(str(exc))
                        if isinstance(ev, StreamComplete) and self._on_done:
                            self._on_done()
                        if isinstance(ev, StreamComplete):
                            self._mode = None
                            self._fallback = []
                            self._profile = None
                            self._overflow = False
                        self._awaiting_done = False
                        self._ws_active = False
                finally:
                    self._busy = False
        except asyncio.CancelledError:
            raise

    async def _handle_event(self, ev: object) -> None:
        gen = self._gen
        if isinstance(ev, StreamStart):
            if self._closing:
                return
            self._failed = False
            self._fallback = []
            if not self._supported:
                self._mode = "fallback"
                return
            timed_out = False
            try:
                await asyncio.wait_for(self._ws_idle.wait(), timeout=self._idle_timeout)
            except asyncio.TimeoutError:
                timed_out = True
            if self._gen != gen:  # idle 待ち中に abort（timeout でも必ず done を出す）
                self._fire_abort_done()
                return
            if timed_out:
                self._failed = True
                self._log("[text] 前発話の終了待ちタイムアウト→破棄")
                if self._on_error:
                    self._on_error("timeout")
                return  # done は後続 StreamComplete が出す
            sid = await self.ws.start_text_stream(
                profile=self._profile, capture=self._capture
            )
            if self._gen != gen:  # start 途中に abort
                self._abort_done_pending = False
                if sid is not None:
                    self._ws_active = True
                    self._awaiting_done = True
                    self._safe_cancel_ws()  # idle(stream_stopped)後に done
                elif self._on_done:
                    self._on_done()  # stream 開始せず → 即 done で reservation 解除
                return
            if sid is None:
                self._failed = True
                self._log("[text] start不可→破棄")
                if self._on_error:
                    self._on_error("start failed (busy/unsupported)")
                return  # done は後続 StreamComplete が出す
            self._ws_active = True
            self._mode = "stream"
        elif isinstance(ev, TextSegment):
            if self._gen != gen or self._failed or self._overflow:
                return
            if self._mode == "fallback":
                self._fallback.append(ev.text)
                return
            if self._ws_active:
                await self.ws.send_text_segment(ev.index, ev.text)
        elif isinstance(ev, StreamComplete):
            if self._gen != gen:  # abort 済み: done は abort/idle 側が出す
                return
            # fire_done=True のまま終われば即 done。実際に upstream が動き出した
            # （speak/end_text_stream が True）場合のみ idle 通知での done に委ねる。
            fire_done = True
            if self._mode == "fallback":
                text = "".join(self._fallback)
                if text.strip():
                    try:
                        await asyncio.wait_for(self._ws_idle.wait(), timeout=self._idle_timeout)
                    except asyncio.TimeoutError:
                        pass
                    if self._gen != gen:
                        self._fire_abort_done()
                        return
                    # idle が speak() return より先に来ても拾えるよう、await の前に立てる。
                    self._awaiting_done = True
                    started = await self.ws.speak(
                        text, self._profile, capture=self._capture
                    )
                    if self._gen != gen:
                        self._abort_done_pending = False
                        if started:
                            self._safe_cancel_ws()  # _awaiting_done は既に True
                        else:
                            self._awaiting_done = False
                            if self._on_done:
                                self._on_done()
                        return
                    if started:
                        fire_done = False  # _awaiting_done 既に True → idle で done
                    elif self._awaiting_done:
                        self._awaiting_done = False  # 開始せず・idle 未消費 → 即 done
                    else:
                        fire_done = False  # await 中に idle が done を出済み（二重防止）
            elif self._ws_active:
                # idle が end() return より先に来ても拾えるよう、await の前に立てる。
                self._awaiting_done = True
                sent = await self.ws.end_text_stream()
                if self._gen != gen:  # end 待ち中に abort: done は abort(idle) 側が出す
                    return
                if sent:
                    fire_done = False
                elif self._awaiting_done:
                    self._awaiting_done = False  # 送れず・idle 未消費 → 即 done
                else:
                    fire_done = False  # await 中に idle が done を出済み（二重防止）
            if fire_done and self._on_done:
                self._on_done()
            self._mode = None
            self._fallback = []
            self._profile = None
            self._overflow = False
