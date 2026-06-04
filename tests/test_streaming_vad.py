"""StreamingVadSegmenter テスト（§5.2）。FakeVAD 注入・GPU/マイク不要。"""
from __future__ import annotations

import struct


from text_to_shaberi.client.audio.vad import (
    StreamComplete,
    StreamStart,
    StreamingVadSegmenter,
    SubSegment,
)


def _frame_bytes(sr: int = 16000, fms: int = 30) -> int:
    return int(sr * fms / 1000 * 2)


def _silence_frame(fb: int) -> bytes:
    return b"\x00" * fb


def _speech_frame(fb: int, amp: int = 5000) -> bytes:
    samples = fb // 2
    return struct.pack(f"<{samples}h", *[amp] * samples)


class FakeVAD:
    def __init__(self, decisions: list[bool] | None = None):
        self._decisions = decisions or []
        self._idx = 0

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        if self._idx < len(self._decisions):
            result = self._decisions[self._idx]
        else:
            result = False
        self._idx += 1
        return result


def _fake_factory(decisions: list[bool]):
    def factory(aggressiveness):
        return FakeVAD(decisions)
    return factory


class TestOnset:
    def test_onset_emits_stream_start(self) -> None:
        sr, fms = 16000, 30
        fb = _frame_bytes(sr, fms)
        onset = 3
        decisions = [False] * 5 + [True] * (onset + 3)
        seg = StreamingVadSegmenter(
            sample_rate=sr, frame_ms=fms, gate=False,
            vad_factory=_fake_factory(decisions),
        )
        pcm = b"".join([_speech_frame(fb)] * len(decisions))
        events = seg.feed(pcm)
        assert any(isinstance(e, StreamStart) for e in events)


class TestTwoLevelSilence:
    def test_subsegment_then_complete(self) -> None:
        sr, fms = 16000, 30
        fb = _frame_bytes(sr, fms)
        seg_silence_frames = 9
        end_silence_frames = 17
        seg_min_frames = 14
        onset = 3

        seg1_speech = seg_min_frames + 5
        seg2_speech = seg_min_frames + 5

        decisions: list[bool] = []
        decisions.extend([False] * 5)
        decisions.extend([True] * (onset + seg1_speech))
        decisions.extend([False] * seg_silence_frames)
        decisions.extend([True] * seg2_speech)
        decisions.extend([False] * end_silence_frames)
        decisions.extend([False] * 10)

        seg = StreamingVadSegmenter(
            sample_rate=sr, frame_ms=fms, gate=False,
            seg_silence_ms=250, end_silence_ms=500, seg_min_ms=400,
            vad_factory=_fake_factory(decisions),
        )

        pcm = b"".join([_speech_frame(fb)] * len(decisions))
        events = seg.feed(pcm)

        subs = [e for e in events if isinstance(e, SubSegment)]
        completes = [e for e in events if isinstance(e, StreamComplete)]

        assert len(subs) >= 2
        assert subs[0].index == 0
        assert subs[1].index == 1
        assert len(subs[0].audio) > 0
        assert len(subs[1].audio) > 0
        assert len(completes) == 1


class TestSegMin:
    def test_tiny_speech_not_emitted(self) -> None:
        sr, fms = 16000, 30
        fb = _frame_bytes(sr, fms)
        onset = 3
        seg_silence_frames = 9
        end_silence_frames = 17
        tiny_speech = 5

        decisions: list[bool] = []
        decisions.extend([False] * 5)
        decisions.extend([True] * (onset + tiny_speech))
        decisions.extend([False] * seg_silence_frames)
        decisions.extend([False] * end_silence_frames)

        seg = StreamingVadSegmenter(
            sample_rate=sr, frame_ms=fms, gate=False,
            seg_silence_ms=250, end_silence_ms=500, seg_min_ms=400,
            vad_factory=_fake_factory(decisions),
        )

        pcm = b"".join([_speech_frame(fb)] * len(decisions))
        events = seg.feed(pcm)

        subs = [e for e in events if isinstance(e, SubSegment)]
        assert len(subs) == 0


class TestSegMax:
    def test_seg_max_forces_split_speaking_continues(self) -> None:
        sr, fms = 16000, 30
        fb = _frame_bytes(sr, fms)
        onset = 3
        seg_max_frames = 20

        total_speech = seg_max_frames + onset + 15
        decisions = [False] * 5 + [True] * total_speech

        seg = StreamingVadSegmenter(
            sample_rate=sr, frame_ms=fms, gate=False,
            seg_silence_ms=250, end_silence_ms=500, seg_min_ms=400,
            seg_max_ms=seg_max_frames * fms,
            vad_factory=_fake_factory(decisions),
        )

        pcm = b"".join([_speech_frame(fb)] * len(decisions))
        events = seg.feed(pcm)

        subs = [e for e in events if isinstance(e, SubSegment)]
        completes = [e for e in events if isinstance(e, StreamComplete)]

        assert len(subs) >= 1
        assert len(completes) == 0
        assert seg._state == "speaking"


class TestFlush:
    def test_flush_emits_sub_then_complete(self) -> None:
        sr, fms = 16000, 30
        fb = _frame_bytes(sr, fms)
        onset = 3
        seg_min_frames = 14

        decisions = [False] * 5 + [True] * (onset + seg_min_frames + 5)

        seg = StreamingVadSegmenter(
            sample_rate=sr, frame_ms=fms, gate=False,
            seg_silence_ms=250, end_silence_ms=500, seg_min_ms=400,
            vad_factory=_fake_factory(decisions),
        )

        pcm = b"".join([_speech_frame(fb)] * len(decisions))
        seg.feed(pcm)

        events = seg.flush()
        subs = [e for e in events if isinstance(e, SubSegment)]
        completes = [e for e in events if isinstance(e, StreamComplete)]

        assert len(subs) == 1
        assert subs[0].index == 0
        assert len(subs[0].audio) > 0
        assert len(completes) == 1
        assert seg._state == "idle"

    def test_flush_idle_returns_empty(self) -> None:
        seg = StreamingVadSegmenter(
            vad_factory=_fake_factory([False] * 5),
        )
        fb = seg._frame_bytes
        seg.feed(b"\x00" * (fb * 5))
        assert seg.flush() == []
