"""test_mic_stream_mode: MicCapture stream_mode=True のイベント経路テスト。

pytest-asyncio 不使用。sounddevice/webrtcvad 不要。
"""
from __future__ import annotations

import asyncio
import threading

import numpy as np

from text_to_shaberi.client.audio.mic import MicCapture
from text_to_shaberi.client.audio.vad import StreamComplete, StreamStart, SubSegment


class FakeStream:
    def __init__(self, blocks, **kwargs):
        self._blocks = list(blocks)

    def start(self):
        pass

    def read(self, frames):
        if not self._blocks:
            raise RuntimeError("stream closed")
        block, overflowed = self._blocks.pop(0)
        return block, overflowed

    def abort(self):
        self._blocks.clear()

    def stop(self):
        pass

    def close(self):
        pass


class FakeStreamSegmenter:
    def __init__(self, feed_results=None, flush_results=None):
        self._feed_results = feed_results or []
        self._flush_results = flush_results or []
        self._feed_idx = 0

    def feed(self, pcm):
        if self._feed_idx < len(self._feed_results):
            result = self._feed_results[self._feed_idx]
            self._feed_idx += 1
            return result
        return []

    def flush(self):
        return list(self._flush_results)

    def reset(self):
        pass


class _ThreadsafeCollector:
    def __init__(self, loop):
        self._loop = loop
        self.items = []
        self._event = threading.Event()

    def __call__(self, item):
        self.items.append(item)
        self._event.set()

    def wait(self, timeout=3.0):
        self._event.wait(timeout)

    def reset_event(self):
        self._event.clear()


def _int16_mono_frame(samples, amplitude=3000):
    return np.full((samples, 1), amplitude, dtype=np.int16)


class FakeSegmenter:
    def __init__(self, feed_results=None, flush_result=None):
        self._feed_results = feed_results or []
        self._flush_result = flush_result
        self._feed_idx = 0

    def feed(self, pcm):
        if self._feed_idx < len(self._feed_results):
            result = self._feed_results[self._feed_idx]
            self._feed_idx += 1
            return result
        return []

    def flush(self):
        return self._flush_result

    def reset(self):
        pass


class TestMicStreamMode:
    def test_stream_events_emitted(self):
        loop = asyncio.new_event_loop()
        collector = _ThreadsafeCollector(loop)

        segmenter = FakeStreamSegmenter(
            feed_results=[
                [StreamStart()],
                [SubSegment(0, b"aa")],
                [SubSegment(1, b"bb")],
            ],
            flush_results=[StreamComplete()],
        )

        frame_samples = 480
        block = _int16_mono_frame(frame_samples)
        blocks = [(block, False)] * 3

        stream = None

        def stream_factory(**kwargs):
            nonlocal stream
            stream = FakeStream(blocks, **kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            on_event=collector,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
            stream_mode=True,
        )

        mic.start()
        collector.wait(timeout=3.0)
        mic.stop()

        pending = loop._ready
        while pending:
            loop._run_once()

        assert len(collector.items) == 4
        assert isinstance(collector.items[0], StreamStart)
        assert isinstance(collector.items[1], SubSegment)
        assert collector.items[1].index == 0
        assert isinstance(collector.items[2], SubSegment)
        assert collector.items[2].index == 1
        assert isinstance(collector.items[3], StreamComplete)
        loop.close()

    def test_utterance_mode_still_works(self):
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
            stream = FakeStream([(block, False)], **kwargs)
            return stream

        mic = MicCapture(
            sample_rate=16000,
            channels=1,
            frame_ms=30,
            on_segment=collector,
            loop=loop,
            stream_factory=stream_factory,
            segmenter_factory=lambda: segmenter,
            stream_mode=False,
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
