"""test_mic_pause: MicCapture pause/resume の fake stream/segmenter テスト。

pause 後は on_segment 発火せず・segmenter.reset() が呼ばれる・resume で再開。
pause 中 stop で flush 尾を emit しない。冪等。
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time

import numpy as np
import pytest

from text_to_shaberi.client.audio.mic import MicCapture


class BlockingFakeStream:
    def __init__(self, **kwargs):
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._started = False

    def start(self):
        self._started = True

    def read(self, frames):
        while True:
            try:
                return self._queue.get(timeout=0.05), False
            except queue.Empty:
                if self._stop_event.is_set():
                    raise RuntimeError("stream closed")

    def abort(self):
        self._stop_event.set()

    def stop(self):
        self._stop_event.set()

    def close(self):
        self._stop_event.set()

    def add_block(self, block):
        self._queue.put(block)


class TrackingSegmenter:
    def __init__(self, feed_results=None, flush_result=None):
        self._feed_results = list(feed_results) if feed_results else []
        self._flush_result = flush_result
        self._feed_idx = 0
        self.feed_calls: list[bytes] = []
        self.flush_called = False
        self.reset_calls = 0
        self._lock = threading.Lock()

    def feed(self, pcm: bytes) -> list[bytes]:
        with self._lock:
            self.feed_calls.append(pcm)
            if self._feed_idx < len(self._feed_results):
                result = self._feed_results[self._feed_idx]
                self._feed_idx += 1
                return result
            return []

    def flush(self) -> bytes | None:
        self.flush_called = True
        return self._flush_result

    def reset(self):
        with self._lock:
            self.reset_calls += 1


def _int16_mono_frame(samples, amplitude=3000):
    return np.full((samples, 1), amplitude, dtype=np.int16)


class _ThreadsafeCollector:
    def __init__(self, loop):
        self._loop = loop
        self.items: list = []
        self._event = threading.Event()

    def __call__(self, item):
        self.items.append(item)
        self._event.set()

    def wait(self, timeout=3.0):
        self._event.wait(timeout)

    def reset_event(self):
        self._event.clear()


def _make_block():
    return _int16_mono_frame(480)


class TestMicPause:
    def test_pause_prevents_segments(self):
        loop = asyncio.new_event_loop()
        collector = _ThreadsafeCollector(loop)
        segmenter = TrackingSegmenter()

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = BlockingFakeStream(**kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            on_segment=collector,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
        )

        mic.start()

        for _ in range(5):
            stream.add_block(_make_block())
        time.sleep(0.1)

        feeds_before_pause = len(segmenter.feed_calls)
        assert feeds_before_pause > 0

        mic.pause()
        assert mic.paused

        for _ in range(5):
            stream.add_block(_make_block())
        time.sleep(0.1)

        feeds_after_pause = len(segmenter.feed_calls)
        assert feeds_after_pause == feeds_before_pause

        mic.stop()
        loop.close()

    def test_pause_calls_segmenter_reset(self):
        loop = asyncio.new_event_loop()
        segmenter = TrackingSegmenter()

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = BlockingFakeStream(**kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
        )

        mic.start()

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        mic.pause()
        time.sleep(0.1)

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        mic.stop()
        assert segmenter.reset_calls >= 1
        loop.close()

    def test_pause_is_idempotent(self):
        loop = asyncio.new_event_loop()
        segmenter = TrackingSegmenter()

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = BlockingFakeStream(**kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
        )

        mic.start()

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        mic.pause()
        mic.pause()
        mic.pause()

        time.sleep(0.1)

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        mic.stop()

        assert segmenter.reset_calls == 1
        loop.close()

    def test_resume_allows_segments_again(self):
        loop = asyncio.new_event_loop()
        seg = b"\x01\x02" * 100
        segmenter = TrackingSegmenter(feed_results=[[seg]] * 20)
        collector = _ThreadsafeCollector(loop)

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = BlockingFakeStream(**kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            on_segment=collector,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
        )

        mic.start()

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        mic.pause()
        time.sleep(0.1)

        feeds_at_pause = len(segmenter.feed_calls)

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        feeds_while_paused = len(segmenter.feed_calls)
        assert feeds_while_paused == feeds_at_pause

        mic.resume()
        assert not mic.paused

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        feeds_after_resume = len(segmenter.feed_calls)
        assert feeds_after_resume > feeds_at_pause

        mic.stop()
        loop.close()

    def test_pause_flush_tail_suppressed_on_stop(self):
        loop = asyncio.new_event_loop()
        flush_seg = b"\x03\x04" * 50
        segmenter = TrackingSegmenter(flush_result=flush_seg)
        collector = _ThreadsafeCollector(loop)

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = BlockingFakeStream(**kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            on_segment=collector,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
        )

        mic.start()

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        mic.pause()
        assert mic.paused

        time.sleep(0.1)
        mic.stop()

        pending = loop._ready
        while pending:
            loop._run_once()

        assert flush_seg not in collector.items
        loop.close()

    def test_normal_flush_tail_emitted_without_pause(self):
        loop = asyncio.new_event_loop()
        flush_seg = b"\x03\x04" * 50
        segmenter = TrackingSegmenter(flush_result=flush_seg)
        collector = _ThreadsafeCollector(loop)

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = BlockingFakeStream(**kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            on_segment=collector,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
        )

        mic.start()

        for _ in range(3):
            stream.add_block(_make_block())
        time.sleep(0.1)

        mic.stop()

        pending = loop._ready
        while pending:
            loop._run_once()

        assert flush_seg in collector.items
        loop.close()

    def test_initial_state_not_paused(self):
        loop = asyncio.new_event_loop()
        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            loop=loop,
            stream_factory=lambda **kw: BlockingFakeStream(**kw),
            segmenter_factory=lambda: TrackingSegmenter(),
        )
        assert not mic.paused
        loop.close()

