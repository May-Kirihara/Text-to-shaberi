"""test_player_lazy_start: StreamingPlayer 遅延スタート・factory注入・Lock 検証。

fake OutputStream / fake resampler を注入し:
- __init__ で start() 未呼出
- 最初の write 前に start() が呼ばれる
- 短発話 close で preroll→tail 順
- request_abort→close の idempotency
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from text_to_shaberi.client.audio.player import StreamingPlayer


class FakeStream:
    def __init__(self, **kwargs):
        self.calls: list[str] = []
        self._latency = 0.01
        self._started = False

    @property
    def latency(self):
        return self._latency

    def start(self):
        self.calls.append("start")
        self._started = True

    def write(self, data):
        self.calls.append(("write", len(data)))

    def stop(self):
        self.calls.append("stop")

    def abort(self):
        self.calls.append("abort")

    def close(self):
        self.calls.append("close")


class FakeResampler:
    def __init__(self, src_rate, target_rate, channels):
        self.calls: list[tuple] = []
        self._src_rate = src_rate
        self._target_rate = target_rate

    def resample_chunk(self, data, last=False):
        self.calls.append(("resample_chunk", len(data), last))
        if last:
            return np.zeros(100, dtype=np.float32)
        if self._target_rate != self._src_rate:
            ratio = self._target_rate / self._src_rate
            new_len = max(1, int(len(data) * ratio))
            return np.zeros(new_len, dtype=np.float32)
        return data


def _make_pcm_s16le(frames: int) -> bytes:
    return (np.random.randint(-32768, 32767, size=frames, dtype="<i2")).tobytes()


class TestLazyStart:
    def test_init_does_not_call_start(self):
        stream = FakeStream()

        def sf(**kwargs):
            return stream

        p = StreamingPlayer(32000, stream_factory=sf, target_sr=48000, preroll_ms=80)
        assert "start" not in stream.calls

    def test_start_called_on_first_preroll_flush(self):
        stream = FakeStream()
        resampler = FakeResampler(32000, 48000, 1)

        def sf(**kwargs):
            return stream

        def rf(sr, tr, ch):
            return resampler

        p = StreamingPlayer(32000, stream_factory=sf, resampler_factory=rf, target_sr=48000, preroll_ms=80)

        big_chunk = _make_pcm_s16le(32000 * 200 // 1000)
        p.write_bytes(big_chunk)

        assert "start" in stream.calls
        write_calls = [c for c in stream.calls if isinstance(c, tuple) and c[0] == "write"]
        assert len(write_calls) >= 1

    def test_short_utterance_close_preroll_then_tail(self):
        stream = FakeStream()
        resampler = FakeResampler(32000, 48000, 1)

        def sf(**kwargs):
            return stream

        def rf(sr, tr, ch):
            return resampler

        p = StreamingPlayer(32000, stream_factory=sf, resampler_factory=rf, target_sr=48000, preroll_ms=80)

        small_chunk = _make_pcm_s16le(100)
        p.write_bytes(small_chunk)
        assert "start" not in stream.calls

        p.close()

        assert "start" in stream.calls
        assert "close" in stream.calls

        write_calls = [c for c in stream.calls if isinstance(c, tuple) and c[0] == "write"]
        assert len(write_calls) == 1

        resample_calls = resampler.calls
        last_calls = [c for c in resample_calls if c[2] is True]
        assert len(last_calls) == 1

    def test_request_abort_then_close_idempotent(self):
        stream = FakeStream()

        def sf(**kwargs):
            return stream

        p = StreamingPlayer(32000, stream_factory=sf, target_sr=32000, preroll_ms=80)

        p.request_abort()
        assert "abort" not in stream.calls

        p.request_abort()

        p.close()
        assert "close" in stream.calls

    def test_abort_idempotent_after_started(self):
        stream = FakeStream()

        def sf(**kwargs):
            return stream

        p = StreamingPlayer(32000, stream_factory=sf, target_sr=48000, preroll_ms=80)

        big_chunk = _make_pcm_s16le(32000 * 200 // 1000)
        p.write_bytes(big_chunk)

        assert "start" in stream.calls

        p.request_abort()
        assert "abort" in stream.calls

        stream.calls.clear()
        p.request_abort()
        assert "abort" not in stream.calls

    def test_no_resampler_same_rate(self):
        stream = FakeStream()

        def sf(**kwargs):
            return stream

        p = StreamingPlayer(32000, stream_factory=sf, target_sr=32000, preroll_ms=80)
        assert p._resampler is None

        big_chunk = _make_pcm_s16le(32000 * 100 // 1000)
        p.write_bytes(big_chunk)
        assert "start" in stream.calls

        p.close()
        assert "close" in stream.calls

    def test_close_without_any_write(self):
        stream = FakeStream()

        def sf(**kwargs):
            return stream

        p = StreamingPlayer(32000, stream_factory=sf, target_sr=32000, preroll_ms=80)
        p.close()
        assert "close" in stream.calls
        assert "stop" not in stream.calls
        assert "start" not in stream.calls
