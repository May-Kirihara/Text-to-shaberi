"""test_mic_capture: MicCapture の fake stream/segmenter 注入テスト。

pytest-asyncio 不使用。sounddevice/webrtcvad 不要。
"""
from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest

from text_to_shaberi.client.audio.mic import MicCapture


class FakeStream:
    def __init__(self, blocks, *, samplerate=16000, channels=1, dtype="int16",
                 device=None, blocksize=480):
        self._blocks = list(blocks)
        self._started = False
        self._aborted = False
        self._stopped = False
        self._closed = False
        self.calls = {"start": 0, "abort": 0, "stop": 0, "close": 0, "read": 0}

    def start(self):
        self._started = True
        self.calls["start"] += 1

    def read(self, frames):
        self.calls["read"] += 1
        if not self._blocks:
            raise RuntimeError("stream closed")
        block, overflowed = self._blocks.pop(0)
        return block, overflowed

    def abort(self):
        self._aborted = True
        self.calls["abort"] += 1
        if self._blocks:
            self._blocks.clear()

    def stop(self):
        self._stopped = True
        self.calls["stop"] += 1

    def close(self):
        self._closed = True
        self.calls["close"] += 1


class FakeSegmenter:
    def __init__(self, feed_results=None, flush_result=None):
        self._feed_results = feed_results or []
        self._flush_result = flush_result
        self._feed_idx = 0
        self.feed_calls: list[bytes] = []
        self.flush_called = False

    def feed(self, pcm: bytes) -> list[bytes]:
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
        pass


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


class TestMicCaptureBasic:
    def test_start_stop_lifecycle(self):
        loop = asyncio.new_event_loop()

        def stream_factory(**kwargs):
            return FakeStream([], **kwargs)

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: FakeSegmenter(),
        )

        assert not mic.active
        mic.stop()
        loop.close()

    def test_segments_emitted_via_call_soon_threadsafe(self):
        loop = asyncio.new_event_loop()
        collector = _ThreadsafeCollector(loop)

        seg1 = b"\x01\x02" * 100

        segmenter = FakeSegmenter(
            feed_results=[[seg1]],
            flush_result=None,
        )

        frame_samples = 480
        block = _int16_mono_frame(frame_samples)

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = FakeStream(
                [(block, False)],
                **kwargs,
            )
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
        collector.wait(timeout=3.0)
        mic.stop()

        pending = loop._ready
        while pending:
            loop._run_once()

        assert len(collector.items) == 1
        assert collector.items[0] == seg1
        loop.close()

    def test_overflowed_log(self):
        loop = asyncio.new_event_loop()
        collector = _ThreadsafeCollector(loop)

        segmenter = FakeSegmenter()

        frame_samples = 480
        block = _int16_mono_frame(frame_samples)

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = FakeStream(
                [(block, True)],
                **kwargs,
            )
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            on_log=collector,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
        )

        mic.start()
        collector.wait(timeout=3.0)
        mic.stop()

        pending = loop._ready
        while pending:
            loop._run_once()

        assert any("オーバーフロー" in l for l in collector.items)
        loop.close()

    def test_stop_is_idempotent(self):
        loop = asyncio.new_event_loop()

        def stream_factory(**kwargs):
            return FakeStream([], **kwargs)

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: FakeSegmenter(),
        )

        mic.stop()
        mic.stop()
        mic.stop()
        loop.close()

    def test_stop_aborts_then_joins_then_closes(self):
        loop = asyncio.new_event_loop()
        stream = None

        frame_samples = 480
        block = _int16_mono_frame(frame_samples)

        def stream_factory(**kwargs):
            nonlocal stream
            stream = FakeStream([(block, False)], **kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: FakeSegmenter(),
        )

        mic.start()
        time.sleep(0.3)
        mic.stop()

        assert stream.calls["abort"] >= 1 or stream.calls["stop"] >= 1
        assert stream.calls["close"] >= 1
        loop.close()

    def test_flush_segment_emitted(self):
        loop = asyncio.new_event_loop()
        collector = _ThreadsafeCollector(loop)

        flush_seg = b"\x03\x04" * 50

        segmenter = FakeSegmenter(
            feed_results=[],
            flush_result=flush_seg,
        )

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = FakeStream([], **kwargs)
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
        collector.wait(timeout=3.0)
        mic.stop()

        pending = loop._ready
        while pending:
            loop._run_once()

        assert flush_seg in collector.items
        loop.close()

    def test_multichannel_mono_conversion(self):
        from text_to_shaberi.client.audio.mic import _to_int16_mono_bytes

        stereo = np.array([[1000, 2000], [3000, 4000]], dtype=np.int16)
        result = _to_int16_mono_bytes(stereo, 2)
        mono = np.frombuffer(result, dtype="<i2")
        assert mono.shape == (2,)
        assert mono[0] == 1500
        assert mono[1] == 3500

    def test_mono_passthrough(self):
        from text_to_shaberi.client.audio.mic import _to_int16_mono_bytes

        mono_in = np.array([1000, 2000], dtype=np.int16).reshape(-1, 1)
        result = _to_int16_mono_bytes(mono_in, 1)
        mono = np.frombuffer(result, dtype="<i2")
        assert mono[0] == 1000
        assert mono[1] == 2000
