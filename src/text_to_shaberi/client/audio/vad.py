"""VadSegmenter: webrtcvad による発話セグメント化（純ロジック・I/O 無し）。

webrtcvad / sounddevice は遅延 import / 注入（テスト時は fake を注入可能）。
numpy はトップレベル可（base 依存）。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

_VALID_SAMPLE_RATES = {8000, 16000, 32000, 48000}
_VALID_FRAME_MS = {10, 20, 30}


def _rms_int16(frame: bytes) -> float:
    samples = np.frombuffer(frame, dtype="<i2")
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


class VadSegmenter:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        aggressiveness: int = 2,
        min_silence_ms: int = 500,
        speech_pad_ms: int = 150,
        min_speech_ms: int = 200,
        max_speech_ms: int = 30000,
        gate: bool = True,
        gate_threshold: int = 200,
        vad_factory: Callable[[int], object] | None = None,
    ) -> None:
        if sample_rate not in _VALID_SAMPLE_RATES:
            raise ValueError(
                f"sample_rate must be one of {sorted(_VALID_SAMPLE_RATES)}, got {sample_rate}"
            )
        if frame_ms not in _VALID_FRAME_MS:
            raise ValueError(
                f"frame_ms must be one of {sorted(_VALID_FRAME_MS)}, got {frame_ms}"
            )

        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._gate = gate
        self._gate_threshold = gate_threshold

        self._frame_bytes = int(sample_rate * frame_ms / 1000 * 2)

        self._pad_frames = max(1, math.ceil(speech_pad_ms / frame_ms))
        self._silence_frames = max(1, math.ceil(min_silence_ms / frame_ms))
        self._onset_frames = 3
        self._min_speech_frames = max(1, math.ceil(min_speech_ms / frame_ms))
        self._max_frames = max(1, math.ceil(max_speech_ms / frame_ms))

        if vad_factory is not None:
            self._vad = vad_factory(aggressiveness)
        else:
            import webrtcvad

            self._vad = webrtcvad.Vad(aggressiveness)

        self._residual = b""
        self.reset()

    def _is_speech(self, frame: bytes) -> bool:
        vad_result = self._vad.is_speech(frame, self._sample_rate)
        if not vad_result:
            return False
        if self._gate:
            return _rms_int16(frame) >= self._gate_threshold
        return True

    def reset(self) -> None:
        self._state = "idle"
        self._seg: bytearray = bytearray()
        self._seg_speech_flags: list[bool] = []
        self._preroll: deque[tuple[bytes, bool]] = deque(maxlen=self._pad_frames)
        self._voiced_run = 0
        self._silence_run = 0
        self._speech_frames = 0

    def feed(self, pcm: bytes) -> list[bytes]:
        buf = self._residual + pcm
        fb = self._frame_bytes
        frames: list[bytes] = []
        start = 0
        while start + fb <= len(buf):
            frames.append(buf[start : start + fb])
            start += fb
        self._residual = buf[start:]

        results: list[bytes] = []
        for frame in frames:
            results.extend(self._process_frame(frame))
        return results

    def flush(self) -> bytes | None:
        self._residual = b""
        if self._state == "speaking" and self._speech_frames >= self._min_speech_frames:
            result = bytes(self._seg)
            self.reset()
            return result
        self.reset()
        return None

    def _process_frame(self, frame: bytes) -> list[bytes]:
        is_speech = self._is_speech(frame)
        results: list[bytes] = []

        if self._state == "idle":
            self._preroll.append((frame, is_speech))
            if is_speech:
                self._voiced_run += 1
            else:
                self._voiced_run = 0

            if self._voiced_run >= self._onset_frames:
                self._seg = bytearray()
                self._seg_speech_flags = []
                self._speech_frames = 0
                for pf_frame, pf_speech in self._preroll:
                    self._seg.extend(pf_frame)
                    self._seg_speech_flags.append(pf_speech)
                    if pf_speech:
                        self._speech_frames += 1
                self._state = "speaking"
                self._silence_run = 0
                self._preroll.clear()
        elif self._state == "speaking":
            self._seg.extend(frame)
            self._seg_speech_flags.append(is_speech)
            if is_speech:
                self._speech_frames += 1
                self._silence_run = 0
            else:
                self._silence_run += 1

            total_frames = len(self._seg_speech_flags)
            if total_frames >= self._max_frames:
                result = bytes(self._seg)
                pad_count = min(self._pad_frames, total_frames)
                seed_flags = self._seg_speech_flags[-pad_count:]
                pad_bytes = self._frame_bytes
                seed = self._seg[-(pad_count * pad_bytes):] if len(self._seg) >= pad_count * pad_bytes else self._seg[:]
                self._seg = bytearray(seed)
                self._seg_speech_flags = list(seed_flags)
                self._silence_run = 0
                self._speech_frames = sum(1 for f in seed_flags if f)
                results.append(result)
            elif self._silence_run >= self._silence_frames:
                if self._speech_frames >= self._min_speech_frames:
                    results.append(bytes(self._seg))
                self.reset()

        return results


@dataclass(slots=True)
class StreamStart:
    pass


@dataclass(slots=True)
class SubSegment:
    index: int
    audio: bytes


@dataclass(slots=True)
class StreamComplete:
    pass


class StreamingVadSegmenter:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        aggressiveness: int = 2,
        seg_silence_ms: int = 250,
        end_silence_ms: int = 500,
        seg_min_ms: int = 400,
        seg_max_ms: int = 2500,
        speech_pad_ms: int = 150,
        gate: bool = True,
        gate_threshold: int = 200,
        vad_factory: Callable[[int], object] | None = None,
    ) -> None:
        if sample_rate not in _VALID_SAMPLE_RATES:
            raise ValueError(
                f"sample_rate must be one of {sorted(_VALID_SAMPLE_RATES)}, got {sample_rate}"
            )
        if frame_ms not in _VALID_FRAME_MS:
            raise ValueError(
                f"frame_ms must be one of {sorted(_VALID_FRAME_MS)}, got {frame_ms}"
            )

        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._gate = gate
        self._gate_threshold = gate_threshold

        self._frame_bytes = int(sample_rate * frame_ms / 1000 * 2)

        self._onset_frames = 3
        self._seg_silence_frames = max(1, math.ceil(seg_silence_ms / frame_ms))
        self._end_silence_frames = max(1, math.ceil(end_silence_ms / frame_ms))
        self._seg_min_frames = max(1, math.ceil(seg_min_ms / frame_ms))
        self._seg_max_frames = max(1, math.ceil(seg_max_ms / frame_ms))
        self._pad_frames = max(1, math.ceil(speech_pad_ms / frame_ms))

        assert self._seg_silence_frames < self._end_silence_frames

        if vad_factory is not None:
            self._vad = vad_factory(aggressiveness)
        else:
            import webrtcvad

            self._vad = webrtcvad.Vad(aggressiveness)

        self._residual = b""
        self.reset()

    def _is_speech(self, frame: bytes) -> bool:
        vad_result = self._vad.is_speech(frame, self._sample_rate)
        if not vad_result:
            return False
        if self._gate:
            return _rms_int16(frame) >= self._gate_threshold
        return True

    def reset(self) -> None:
        self._state = "idle"
        self._seg: bytearray = bytearray()
        self._seg_speech_frames: int = 0
        self._preroll: deque[tuple[bytes, bool]] = deque(maxlen=self._pad_frames)
        self._voiced_run: int = 0
        self._silence_run: int = 0
        self._index: int = 0

    def feed(self, pcm: bytes) -> list:
        buf = self._residual + pcm
        fb = self._frame_bytes
        frames: list[bytes] = []
        start = 0
        while start + fb <= len(buf):
            frames.append(buf[start : start + fb])
            start += fb
        self._residual = buf[start:]

        events: list = []
        for frame in frames:
            events.extend(self._process_frame(frame))
        return events

    def flush(self) -> list:
        self._residual = b""
        events: list = []
        if self._state in ("speaking", "gap"):
            if self._seg_speech_frames >= self._seg_min_frames:
                events.append(SubSegment(index=self._index, audio=bytes(self._seg)))
                self._index += 1
            events.append(StreamComplete())
            self.reset()
        return events

    def _process_frame(self, frame: bytes) -> list:
        is_speech = self._is_speech(frame)
        events: list = []

        if self._state == "idle":
            self._preroll.append((frame, is_speech))
            if is_speech:
                self._voiced_run += 1
            else:
                self._voiced_run = 0

            if self._voiced_run >= self._onset_frames:
                self._seg = bytearray()
                self._seg_speech_frames = 0
                for pf_frame, pf_speech in self._preroll:
                    self._seg.extend(pf_frame)
                    if pf_speech:
                        self._seg_speech_frames += 1
                self._state = "speaking"
                self._silence_run = 0
                self._preroll.clear()
                self._index = 0
                events.append(StreamStart())

        elif self._state == "speaking":
            self._seg.extend(frame)
            if is_speech:
                self._seg_speech_frames += 1
                self._silence_run = 0
            else:
                self._silence_run += 1

            total_frames = len(self._seg) // self._frame_bytes

            if total_frames >= self._seg_max_frames:
                events.append(SubSegment(index=self._index, audio=bytes(self._seg)))
                self._index += 1
                pad_bytes = self._pad_frames * self._frame_bytes
                seed = self._seg[-pad_bytes:] if len(self._seg) >= pad_bytes else self._seg[:]
                self._seg = bytearray(seed)
                self._seg_speech_frames = 0
                self._silence_run = 0
            elif self._silence_run >= self._seg_silence_frames:
                if self._seg_speech_frames >= self._seg_min_frames:
                    events.append(SubSegment(index=self._index, audio=bytes(self._seg)))
                    self._index += 1
                    self._seg = bytearray()
                    self._seg_speech_frames = 0
                self._state = "gap"
                self._silence_run = 0

        elif self._state == "gap":
            if is_speech:
                self._seg.extend(frame)
                self._seg_speech_frames += 1
                self._silence_run = 0
                self._state = "speaking"
            else:
                self._silence_run += 1
                if self._silence_run >= self._end_silence_frames:
                    events.append(StreamComplete())
                    self.reset()

        return events
