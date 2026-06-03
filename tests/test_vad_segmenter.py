"""test_vad_segmenter: VadSegmenter の fake VAD 注入テスト。

pytest-asyncio 不使用。webrtcvad/sounddevice 不要。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from text_to_shaberi.client.audio.vad import VadSegmenter


def _make_frame_int16(samples: int, amplitude: int = 0) -> bytes:
    return struct.pack(f"<{samples}h", *[amplitude] * samples)


def _silence_frame(frame_bytes: int) -> bytes:
    return b"\x00" * frame_bytes


def _speech_frame(frame_bytes: int, amplitude: int = 5000) -> bytes:
    samples = frame_bytes // 2
    return struct.pack(f"<{samples}h", *[amplitude] * samples)


class FakeVAD:
    def __init__(self, frame_decisions: list[bool] | None = None):
        self._decisions = frame_decisions or []
        self._idx = 0

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        if self._idx < len(self._decisions):
            result = self._decisions[self._idx]
        else:
            result = False
        self._idx += 1
        return result


class CountingFakeVAD:
    def __init__(self):
        self.call_count = 0

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        self.call_count += 1
        return True


def _fake_vad_factory_with_decisions(decisions: list[bool]):
    def factory(aggressiveness):
        return FakeVAD(decisions)
    return factory


class TestVadSegmenterParams:
    def test_invalid_sample_rate_raises(self):
        with pytest.raises(ValueError, match="sample_rate"):
            VadSegmenter(sample_rate=44100, vad_factory=lambda a: FakeVAD())

    def test_invalid_frame_ms_raises(self):
        with pytest.raises(ValueError, match="frame_ms"):
            VadSegmenter(frame_ms=25, vad_factory=lambda a: FakeVAD())

    def test_valid_params_ok(self):
        seg = VadSegmenter(sample_rate=16000, frame_ms=30, vad_factory=lambda a: FakeVAD())
        assert seg._frame_bytes == 960

    def test_valid_params_8k_10ms(self):
        seg = VadSegmenter(sample_rate=8000, frame_ms=10, vad_factory=lambda a: FakeVAD())
        assert seg._frame_bytes == 160


class TestVadSegmenterBasic:
    def test_silence_then_speech_then_silence(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)
        onset_frames = 3
        silence_frames = 17
        pad_frames = 5

        decisions = []
        decisions.extend([False] * 10)
        decisions.extend([True] * (onset_frames + 5))
        decisions.extend([False] * silence_frames)
        decisions.extend([False] * 5)

        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            min_silence_ms=500,
            speech_pad_ms=150,
            min_speech_ms=200,
            gate=False,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        pcm = b"".join([_speech_frame(fb, 3000)] * len(decisions))
        results = seg.feed(pcm)
        assert len(results) == 1
        result = results[0]
        assert len(result) > 0
        expected_min = pad_frames * fb + onset_frames * fb
        assert len(result) >= expected_min

    def test_short_burst_discarded(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)
        onset = 3

        decisions = [False] * 5
        decisions.extend([True] * (onset + 1))
        decisions.extend([False] * 20)

        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            min_speech_ms=200,
            speech_pad_ms=150,
            gate=False,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        pcm = b"".join([_speech_frame(fb, 3000)] * len(decisions))
        results = seg.feed(pcm)
        assert len(results) == 0

    def test_max_speech_forced_split_with_continuation(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)
        max_frames = 10
        pad_frames = 5

        total_frames = 25
        decisions = [True] * total_frames

        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            min_silence_ms=500,
            speech_pad_ms=150,
            min_speech_ms=200,
            max_speech_ms=max_frames * fms,
            gate=False,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        pcm = b"".join([_speech_frame(fb, 3000)] * total_frames)
        results = seg.feed(pcm)

        assert len(results) >= 2
        assert len(results[0]) == max_frames * fb

        total_from_results = sum(len(r) for r in results)
        remaining_in_seg = len(seg._seg)
        assert total_from_results + remaining_in_seg >= total_frames * fb

    def test_flush_returns_remaining(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)
        onset = 3

        decisions = [False] * 5
        decisions.extend([True] * (onset + 5))

        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            min_speech_ms=200,
            gate=False,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        pcm = b"".join([_speech_frame(fb, 3000)] * len(decisions))
        seg.feed(pcm)

        tail = seg.flush()
        assert tail is not None
        assert len(tail) > 0

    def test_flush_returns_none_when_idle(self):
        seg = VadSegmenter(
            vad_factory=_fake_vad_factory_with_decisions([False] * 5),
        )
        fb = seg._frame_bytes
        pcm = b"\x00" * (fb * 5)
        seg.feed(pcm)
        assert seg.flush() is None

    def test_residual_carried_over(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)

        decisions = [False] * 20
        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            gate=False,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        half = fb // 2
        seg.feed(b"\x00" * half)
        assert len(seg._residual) == half

        seg.feed(b"\x00" * (fb - half + fb * 2))
        assert len(seg._residual) < fb

    def test_gate_blocks_low_rms(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)
        onset = 3
        silence_frames = 17

        decisions = [False] * 5
        decisions.extend([True] * (onset_frames := onset + 5))
        decisions.extend([False] * silence_frames)

        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            gate=True,
            gate_threshold=500,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        low_amp = 10
        pcm = b"".join([_speech_frame(fb, low_amp)] * len(decisions))
        results = seg.feed(pcm)
        assert len(results) == 0

    def test_gate_passes_high_rms(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)
        onset = 3
        silence_frames = 17

        decisions = [False] * 5
        decisions.extend([True] * (onset + 5))
        decisions.extend([False] * silence_frames)

        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            gate=True,
            gate_threshold=200,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        pcm = b"".join([_speech_frame(fb, 5000)] * len(decisions))
        results = seg.feed(pcm)
        assert len(results) == 1

    def test_reset_clears_state(self):
        sr = 16000
        fms = 30
        fb = int(sr * fms / 1000 * 2)
        onset = 3

        decisions = [False] * 5
        decisions.extend([True] * (onset + 5))

        seg = VadSegmenter(
            sample_rate=sr,
            frame_ms=fms,
            gate=False,
            vad_factory=_fake_vad_factory_with_decisions(decisions),
        )

        pcm = b"".join([_speech_frame(fb, 3000)] * len(decisions))
        seg.feed(pcm)
        seg.reset()
        assert seg._state == "idle"
        assert len(seg._seg) == 0
        assert len(seg._preroll) == 0
