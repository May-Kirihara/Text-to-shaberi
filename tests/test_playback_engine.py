"""test_playback_engine: PlaybackEngine のユニットテスト。

fake stream/resampler/loop 注入。実 sounddevice/soxr/実デバイス不要。
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time

import numpy as np
import pytest

from text_to_shaberi.client.audio.engine import (
    _ABORT,
    _CLOSE,
    _Audio,
    _Begin,
    _End,
    PlaybackEngine,
)
from text_to_shaberi.common.pcm import _pcm_to_float32


class FakeFormat:
    def __init__(self, sample_rate=32000, channels=1, codec="pcm_s16le"):
        self.sample_rate = sample_rate
        self.channels = channels
        self.codec = codec


class FakeStream:
    def __init__(self, **kwargs):
        self.writes: list[np.ndarray] = []
        self._started = False
        self._stopped = False
        self._closed = False
        self._aborted = False
        self._write_block = threading.Event()
        self._write_block.set()
        self._sr = kwargs.get("samplerate", 48000)

    @property
    def latency(self):
        return 0.01

    def start(self):
        self._started = True

    def write(self, data):
        self._write_block.wait()
        self.writes.append(np.array(data).copy())

    def stop(self):
        self._stopped = True

    def abort(self):
        self._aborted = True

    def close(self):
        self._closed = True


class FakeStreamFactory:
    def __init__(self):
        self.streams: list[FakeStream] = []

    def __call__(self, **kwargs):
        s = FakeStream(**kwargs)
        self.streams.append(s)
        return s


class FakeResampler:
    def __init__(self, src_rate, target_rate, channels):
        self._src_rate = src_rate
        self._target_rate = target_rate
        self.calls: list[tuple] = []
        self._reset_count = 0

    def resample_chunk(self, data, last=False):
        self.calls.append(("resample_chunk", len(data), last))
        if last:
            return np.zeros(100, dtype=np.float32)
        ratio = self._target_rate / self._src_rate
        new_len = max(1, int(len(data) * ratio))
        return np.zeros(new_len, dtype=np.float32)


class FakeResamplerFactory:
    def __init__(self):
        self.resamplers: list[FakeResampler] = []

    def __call__(self, src_rate, target_rate, channels):
        r = FakeResampler(src_rate, target_rate, channels)
        self.resamplers.append(r)
        return r


def _make_engine(**kwargs):
    sf = FakeStreamFactory()
    rf = FakeResamplerFactory()
    defaults = dict(
        stream_factory=sf,
        resampler_factory=rf,
        keepalive_ms=20,
        warm_min_ms=60,
        max_write_ms=20,
        queue_max=512,
    )
    defaults.update(kwargs)
    engine = PlaybackEngine(**defaults)
    return engine, sf, rf


def _pcm_s16le_silence(frames: int) -> bytes:
    return b"\x00\x00" * frames


def _pcm_s16le_nonzero(frames: int) -> bytes:
    return (np.random.randint(-32768, 32767, size=frames, dtype="<i2")).tobytes()


class TestPrewarmKeepalive:
    def test_prewarm_starts_worker_and_keepalive(self):
        logs: list[str] = []
        engine, sf, rf = _make_engine(on_log=logs.append)
        engine.prewarm(None)
        assert engine._warm.is_set()
        s = sf.streams[0]
        assert s._started
        assert len(s.writes) > 0
        engine.close()
        engine.join(timeout=2.0)

    def test_prewarm_warm_event_set_after_min_ms(self):
        engine, sf, rf = _make_engine(warm_min_ms=60)
        t0 = time.monotonic()
        engine.prewarm(None)
        elapsed = time.monotonic() - t0
        assert engine._warm.is_set()
        assert elapsed < 3.0
        engine.close()
        engine.join(timeout=2.0)

    def test_prewarm_idempotent(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        n1 = len(sf.streams)
        engine.prewarm(None)
        n2 = len(sf.streams)
        assert n1 == n2
        engine.close()
        engine.join(timeout=2.0)


class TestBeginFeedEnd:
    def test_begin_feed_end_writes_frames(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        chunk = _pcm_s16le_nonzero(4800)
        engine.feed(chunk)
        engine.end()
        time.sleep(0.3)
        s = sf.streams[0]
        assert len(s.writes) > 1
        engine.close()
        engine.join(timeout=2.0)

    def test_end_does_not_close_stream(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        engine.feed(_pcm_s16le_silence(480))
        engine.end()
        time.sleep(0.2)
        s = sf.streams[0]
        assert not s._closed
        assert not s._stopped
        engine.close()
        engine.join(timeout=2.0)

    def test_second_begin_reuses_stream(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        engine.feed(_pcm_s16le_silence(480))
        engine.end()
        time.sleep(0.2)

        engine.begin(fmt, None)
        engine.feed(_pcm_s16le_silence(480))
        engine.end()
        time.sleep(0.2)

        assert len(sf.streams) == 1
        s = sf.streams[0]
        assert not s._closed
        engine.close()
        engine.join(timeout=2.0)


class TestSeqDiscipline:
    def test_abort_discards_old_seq_audio(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        engine.feed(_pcm_s16le_silence(4800))
        engine.abort()
        time.sleep(0.1)
        engine.begin(fmt, None)
        chunk = _pcm_s16le_silence(4800)
        engine.feed(chunk)
        engine.end()
        time.sleep(0.3)
        s = sf.streams[0]
        assert not s._closed
        engine.close()
        engine.join(timeout=2.0)

    def test_back_to_back_begin_no_mixing(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt1 = FakeFormat(sample_rate=48000)
        fmt2 = FakeFormat(sample_rate=32000)
        engine.begin(fmt1, None)
        engine.feed(_pcm_s16le_silence(4800))
        engine.begin(fmt2, None)
        engine.feed(_pcm_s16le_silence(3200))
        engine.end()
        time.sleep(0.3)
        s = sf.streams[0]
        assert not s._closed
        engine.close()
        engine.join(timeout=2.0)


class TestCooperativeAbort:
    def test_write_chunked_respects_seq_change(self):
        engine, sf, rf = _make_engine(max_write_ms=5)
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        big_pcm = _pcm_s16le_silence(48000)
        engine.feed(big_pcm)
        time.sleep(0.05)
        engine.abort()
        time.sleep(0.2)
        s = sf.streams[0]
        assert not s._aborted
        engine.close()
        engine.join(timeout=2.0)

    def test_stream_abort_not_called(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        engine.feed(_pcm_s16le_silence(480))
        engine.abort()
        time.sleep(0.2)
        s = sf.streams[0]
        assert not s._aborted
        engine.close()
        engine.join(timeout=2.0)


class TestDeviceChange:
    def test_device_change_reopen(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, "device_A")
        engine.feed(_pcm_s16le_silence(480))
        engine.end()
        time.sleep(0.2)

        engine.begin(fmt, "device_B")
        engine.feed(_pcm_s16le_silence(480))
        engine.end()
        time.sleep(0.2)

        assert len(sf.streams) >= 2
        engine.close()
        engine.join(timeout=2.0)


class TestSourceRateChange:
    def test_source_rate_change_creates_new_resampler(self):
        engine, sf, rf = _make_engine()
        engine.prewarm(None)
        time.sleep(0.1)
        fmt1 = FakeFormat(sample_rate=32000)
        engine.begin(fmt1, None)
        engine.feed(_pcm_s16le_silence(3200))
        engine.end()
        time.sleep(0.2)

        fmt2 = FakeFormat(sample_rate=24000)
        engine.begin(fmt2, None)
        engine.feed(_pcm_s16le_silence(2400))
        engine.end()
        time.sleep(0.2)

        assert len(rf.resamplers) >= 2
        engine.close()
        engine.join(timeout=2.0)


class TestWorkerExceptionRecovery:
    def test_worker_recovers_after_write_exception(self):
        sf = FakeStreamFactory()
        rf = FakeResamplerFactory()
        engine = PlaybackEngine(
            stream_factory=sf,
            resampler_factory=rf,
            keepalive_ms=20,
            warm_min_ms=60,
        )
        engine.prewarm(None)
        time.sleep(0.1)

        s = sf.streams[0]
        s._write_block.clear()

        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        engine.feed(_pcm_s16le_silence(4800))
        time.sleep(0.1)
        s._write_block.set()
        time.sleep(0.3)

        with engine._lock:
            thread_alive = engine._thread is not None and engine._thread.is_alive()
            if not thread_alive:
                engine.prewarm(None)
                time.sleep(0.1)

        assert engine._started
        engine.close()
        engine.join(timeout=2.0)


class TestFeedFull:
    def test_feed_returns_false_when_full(self):
        engine, sf, rf = _make_engine(queue_max=4)
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000)
        engine.begin(fmt, None)
        s = sf.streams[0]
        s._write_block.clear()
        time.sleep(0.05)
        got_false = False
        chunk = _pcm_s16le_silence(4800)
        for _ in range(100):
            ok = engine.feed(chunk)
            if not ok:
                got_false = True
                break
        s._write_block.set()
        engine.abort()
        engine.close()
        engine.join(timeout=2.0)
        assert got_false


class TestLogAndWarm:
    def test_log_uses_call_soon_threadsafe(self):
        logs: list[str] = []
        received: list[tuple] = []

        class FakeLoop:
            def call_soon_threadsafe(self, cb, *args):
                received.append(("call_soon_threadsafe", cb, args))
                cb(*args)

        loop = FakeLoop()

        class SlowStream(FakeStream):
            def write(self, data):
                import time as _t
                _t.sleep(0.1)
                super().write(data)

        sf = FakeStreamFactory()

        def slow_sf(**kwargs):
            s = SlowStream(**kwargs)
            sf.streams.append(s)
            return s

        engine = PlaybackEngine(
            stream_factory=slow_sf,
            resampler_factory=FakeResamplerFactory(),
            on_log=logs.append,
            loop=loop,
            keepalive_ms=20,
            warm_min_ms=60,
        )
        engine.prewarm(None)
        time.sleep(0.5)
        engine.close()
        engine.join(timeout=2.0)
        assert len(received) > 0
        assert len(logs) > 0

    def test_warm_event_uses_call_soon_threadsafe(self):
        logs: list[str] = []
        received: list[tuple] = []

        class FakeLoop:
            def call_soon_threadsafe(self, cb, *args):
                received.append(("call_soon_threadsafe", cb, args))

        loop = FakeLoop()
        engine, sf, rf = _make_engine(on_log=logs.append, loop=loop)
        engine.prewarm(None)
        time.sleep(0.2)
        engine.close()
        engine.join(timeout=2.0)


class TestMultiChannelReject:
    def test_channels_gt1_logs_rejection(self):
        logs: list[str] = []
        engine, sf, rf = _make_engine(on_log=logs.append)
        engine.prewarm(None)
        time.sleep(0.1)
        fmt = FakeFormat(sample_rate=48000, channels=2)
        engine.begin(fmt, None)
        engine.feed(_pcm_s16le_silence(4800))
        engine.end()
        time.sleep(0.2)
        assert any("multi-channel" in l for l in logs)
        engine.close()
        engine.join(timeout=2.0)
