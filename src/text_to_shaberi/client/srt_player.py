"""SRT 再生スケジューラ。"""
from __future__ import annotations

import asyncio


class SrtPlayer:
    def __init__(self, loop, ws_client, on_state_change=None):
        self._loop = loop
        self._ws_client = ws_client
        # on_state_change は将来の拡張用。現在は play(on_entry=...) を使用。
        self._on_state_change = on_state_change

        self._idle_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop_flag = False
        self._srt_playing = False
        self._paused_cumulative = 0.0

    @property
    def is_playing(self) -> bool:
        return self._srt_playing

    async def play(self, entries, profile=None, on_entry=None, wait_for_playback=False):
        self._srt_playing = True
        self._stop_flag = False
        self._paused_cumulative = 0.0

        original_on_state = getattr(self._ws_client, "_on_state", None)
        self._idle_event.clear()

        def _wrapped_on_state(event, payload=None):
            if event == "idle":
                self._idle_event.set()
            if original_on_state:
                original_on_state(event, payload)

        self._ws_client._on_state = _wrapped_on_state

        try:
            t_origin = self._loop.time()
            total = len(entries)

            for i, entry in enumerate(entries):
                if self._stop_flag:
                    break

                if not entry.text.strip():
                    continue

                if on_entry:
                    on_entry(i, total, entry)

                virtual_t = (self._loop.time() - self._paused_cumulative) - t_origin
                target = entry.start_ms / 1000.0
                wait = target - virtual_t

                if wait > 0:
                    if not await self._wait_with_pause(wait):
                        break

                self._idle_event.clear()
                ok = await self._ws_client.speak(entry.text, profile)
                if not ok:
                    break

                await self._idle_event.wait()

                if self._stop_flag:
                    break

                if wait_for_playback:
                    remaining = self._ws_client._last_playback_remaining
                    if remaining > 0:
                        if not await self._wait_with_pause(remaining):
                            break

                # pause中ならここで待機（cancel後の即進行防止）
                if not self._pause_event.is_set():
                    await self._pause_event.wait()
        finally:
            self._srt_playing = False
            self._ws_client._on_state = original_on_state

    async def _wait_with_pause(self, seconds):
        deadline = self._loop.time() + seconds
        while self._loop.time() < deadline:
            if self._stop_flag:
                return False
            if not self._pause_event.is_set():
                pause_start = self._loop.time()
                await self._pause_event.wait()
                paused_duration = self._loop.time() - pause_start
                self._paused_cumulative += paused_duration
                deadline += paused_duration
            remaining = deadline - self._loop.time()
            if remaining > 0.02:
                await asyncio.sleep(min(remaining, 0.05))
            else:
                break
        return True

    def pause(self):
        self._pause_event.clear()
        if self._ws_client._active_id is not None:
            asyncio.ensure_future(self._ws_client.cancel())

    def resume(self):
        self._pause_event.set()

    async def stop(self):
        self._stop_flag = True
        self._idle_event.set()
        self._pause_event.set()
        await self._ws_client.cancel()
