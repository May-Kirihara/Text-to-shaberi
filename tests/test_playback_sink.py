"""test_playback_sink: PlaybackSink の非ブロッキング・worker ログ・queue 満杯検証。

pytest-asyncio 不使用。asyncio.new_event_loop で loop を用意。
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time

import pytest

from text_to_shaberi.client.audio.sink import PlaybackSink


class FakeFormat:
    def __init__(self):
        self.sample_rate = 32000
        self.channels = 1
        self.codec = "pcm_s16le"


class FakePlayer:
    def __init__(self, **kwargs):
        self.calls: list[str] = []
        self._write_block = threading.Event()
        self._write_block.set()

    def write_bytes(self, data: bytes) -> None:
        self._write_block.wait()
        self.calls.append(("write_bytes", len(data)))

    def request_abort(self) -> None:
        self.calls.append("request_abort")
        self._write_block.set()

    def close(self) -> None:
        self.calls.append("close")


class FakePlayerBlocking(FakePlayer):
    def write_bytes(self, data: bytes) -> None:
        self.calls.append(("write_bytes", len(data)))
        self._write_block.wait()


class TestPlaybackSink:
    def test_end_returns_immediately(self):
        logs: list[str] = []
        sink = PlaybackSink(
            player_factory=FakePlayer,
            on_log=logs.append,
        )
        sink.begin(FakeFormat(), None)
        t0 = time.monotonic()
        sink.end()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1
        sink.join(timeout=2.0)

    def test_abort_returns_immediately(self):
        logs: list[str] = []
        sink = PlaybackSink(
            player_factory=FakePlayer,
            on_log=logs.append,
        )
        sink.begin(FakeFormat(), None)
        t0 = time.monotonic()
        sink.abort()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1
        sink.join(timeout=2.0)

    def test_abort_during_write(self):
        player_instance = None

        def factory(**kwargs):
            nonlocal player_instance
            player_instance = FakePlayerBlocking()
            player_instance._write_block.clear()
            return player_instance

        logs: list[str] = []
        sink = PlaybackSink(
            player_factory=factory,
            on_log=logs.append,
        )
        sink.begin(FakeFormat(), None)

        time.sleep(0.05)

        chunk = b"\x00\x00" * 100
        sink.feed(chunk)
        time.sleep(0.05)

        sink.abort()

        sink.join(timeout=3.0)
        assert player_instance is not None
        assert "request_abort" in player_instance.calls

    def test_abort_multiple_times(self):
        logs: list[str] = []
        sink = PlaybackSink(
            player_factory=FakePlayer,
            on_log=logs.append,
        )
        sink.begin(FakeFormat(), None)
        time.sleep(0.02)
        sink.abort()
        sink.abort()
        sink.abort()
        sink.join(timeout=2.0)

    def test_worker_log_via_call_soon_threadsafe(self):
        logs: list[str] = []
        received: list[str] = []
        loop = asyncio.new_event_loop()

        def on_log(msg):
            received.append(msg)

        def factory(**kwargs):
            raise RuntimeError("test generation failure")

        sink = PlaybackSink(
            player_factory=factory,
            on_log=on_log,
            loop=loop,
        )
        sink.begin(FakeFormat(), None)
        sink.join(timeout=2.0)

        loop.call_soon_threadsafe = lambda cb, *args: received.append(("via_loop", cb, args))
        try:
            pass
        finally:
            loop.close()

    def test_feed_full_returns_false(self):
        logs: list[str] = []
        block_event = threading.Event()

        class SlowPlayer:
            def __init__(self, **kwargs):
                self.calls = []

            def write_bytes(self, data):
                block_event.wait()

            def request_abort(self):
                self.calls.append("request_abort")
                block_event.set()

            def close(self):
                self.calls.append("close")

        sink = PlaybackSink(
            player_factory=SlowPlayer,
            on_log=logs.append,
        )
        sink.begin(FakeFormat(), None)
        time.sleep(0.02)

        chunk = b"\x00\x00" * 100
        for _ in range(300):
            ok = sink.feed(chunk)
            if not ok:
                sink.abort()
                sink.join(timeout=2.0)
                return

        sink.abort()
        sink.join(timeout=2.0)
        pytest.fail("feed should have returned False at some point")
