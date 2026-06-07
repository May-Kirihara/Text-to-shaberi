"""test_srt_player: SrtPlayer 再生スケジューラのテスト。

対象: text_to_shaberi.client.srt_player（未実装・TDD）
pytest-asyncio 不使用。asyncio.run で完結。
"""
from __future__ import annotations

import asyncio
import time
import uuid

from text_to_shaberi.client.srt_parser import SubtitleEntry
from text_to_shaberi.client.srt_player import SrtPlayer


def _run_test(coroutine):
    return asyncio.run(coroutine)


class FakeWSClient:
    def __init__(self, loop, *, speak_delay=0.05):
        self._loop = loop
        self._active_id = None
        self._connected = True
        self.speak_log: list[tuple[str, str | None]] = []
        self.cancel_log: list[str] = []
        self._speak_delay = speak_delay
        self._on_state = None

    async def speak(self, text, profile=None):
        if self._active_id is not None or not self._connected:
            return False
        self._active_id = str(uuid.uuid4())
        self.speak_log.append((text, profile))
        self._loop.call_later(self._speak_delay, self._emit_idle)
        return True

    def _emit_idle(self):
        self._active_id = None
        if self._on_state:
            self._on_state("idle")

    async def cancel(self):
        if self._active_id:
            self.cancel_log.append(self._active_id)
            self._active_id = None
            if self._on_state:
                self._on_state("idle")

    def is_connected(self):
        return self._connected

    def set_on_state(self, cb):
        self._on_state = cb


def _make_entries(*specs: tuple[int, int, str]) -> list[SubtitleEntry]:
    return [
        SubtitleEntry(index=i + 1, start_ms=spec[0], end_ms=spec[1], text=spec[2])
        for i, spec in enumerate(specs)
    ]


class TestSrtPlayerPlay:
    def test_three_entries_sequential(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 500, "A"),
                (600, 1000, "B"),
                (1100, 1500, "C"),
            )
            await player.play(entries, profile="test-profile")
            assert len(ws.speak_log) == 3
            assert ws.speak_log[0] == ("A", "test-profile")
            assert ws.speak_log[1] == ("B", "test-profile")
            assert ws.speak_log[2] == ("C", "test-profile")

        _run_test(go())

    def test_gap_timing(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop, speak_delay=0.02)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 100, "first"),
                (300, 400, "second"),
                (800, 900, "third"),
            )
            t0 = time.monotonic()
            await player.play(entries)
            elapsed = time.monotonic() - t0
            assert elapsed >= 0.6
            assert len(ws.speak_log) == 3

        _run_test(go())

    def test_no_gap_immediate(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop, speak_delay=0.02)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 100, "A"),
                (0, 100, "B"),
            )
            await player.play(entries)
            assert len(ws.speak_log) == 2

        _run_test(go())

    def test_empty_text_skipped(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 100, ""),
                (100, 200, "B"),
            )
            await player.play(entries)
            assert len(ws.speak_log) == 1
            assert ws.speak_log[0][0] == "B"

        _run_test(go())

    def test_on_entry_callback(self):
        callbacks = []

        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop, speak_delay=0.02)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 100, "A"),
                (200, 300, "B"),
                (400, 500, "C"),
            )

            def cb(idx, total, entry):
                callbacks.append((idx, total, entry.text))

            await player.play(entries, on_entry=cb)
            assert len(callbacks) == 3
            assert callbacks[0] == (0, 3, "A")
            assert callbacks[1] == (1, 3, "B")
            assert callbacks[2] == (2, 3, "C")

        _run_test(go())


class TestSrtPlayerControl:
    def test_stop_cancels_and_ends_play(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop, speak_delay=0.5)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 1000, "long"),
                (2000, 3000, "after"),
            )

            async def _stop_after_delay():
                await asyncio.sleep(0.2)
                await player.stop()

            loop.create_task(_stop_after_delay())
            await player.play(entries)
            assert len(ws.cancel_log) >= 1
            assert not player.is_playing

        _run_test(go())

    def test_play_after_stop_restarts(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop, speak_delay=0.02)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 100, "first"),
                (500, 600, "second"),
            )
            await player.play(entries)
            assert len(ws.speak_log) == 2

            ws.speak_log.clear()
            await player.play(entries)
            assert len(ws.speak_log) == 2

        _run_test(go())

    def test_speak_false_stops_play(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop)
            ws._connected = False
            player = SrtPlayer(loop, ws)
            entries = _make_entries((0, 100, "A"))
            await player.play(entries)
            assert len(ws.speak_log) == 0
            assert not player.is_playing

        _run_test(go())

    def test_is_playing_flag(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop, speak_delay=0.1)
            player = SrtPlayer(loop, ws)
            entries = _make_entries((0, 100, "A"))
            assert not player.is_playing

            play_task = loop.create_task(player.play(entries))
            await asyncio.sleep(0.01)
            assert player.is_playing

            await play_task
            assert not player.is_playing

        _run_test(go())


class TestSrtPlayerPauseResume:
    def test_pause_resume(self):
        async def go():
            loop = asyncio.get_event_loop()
            ws = FakeWSClient(loop, speak_delay=0.02)
            player = SrtPlayer(loop, ws)
            entries = _make_entries(
                (0, 100, "A"),
                (300, 400, "B"),
                (600, 700, "C"),
            )

            async def _pause_resume():
                await asyncio.sleep(0.05)
                player.pause()
                await asyncio.sleep(0.15)
                player.resume()

            loop.create_task(_pause_resume())
            await player.play(entries)
            assert len(ws.speak_log) == 3

        _run_test(go())
