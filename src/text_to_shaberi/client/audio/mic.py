"""MicCapture: sounddevice InputStream によるブロッキング読み取り + VadSegmenter。

sounddevice は遅延 import（factory 内）。server/torch/qwen/silero は import 禁止。
出力は常に mono int16 LE bytes（多ch は平均で mono 化）。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

import numpy as np


def _to_int16_mono_bytes(data: np.ndarray, channels: int) -> bytes:
    if data.ndim == 2 and channels > 1:
        mono = np.mean(data, axis=1)
        mono = np.clip(mono, -32768, 32767).astype(np.int16)
    else:
        mono = data.ravel().astype(np.int16) if data.dtype != np.int16 else data.ravel()
    arr = np.ascontiguousarray(mono)
    return arr.tobytes()


class MicCapture:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        device: int | None = None,
        frame_ms: int = 30,
        vad_config: Any = None,
        on_segment: Callable[[bytes], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        loop: Any | None = None,
        stream_factory: Callable[..., Any] | None = None,
        segmenter_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._device = device
        self._frame_ms = frame_ms
        self._on_segment = on_segment
        self._on_log = on_log
        self._loop = loop
        self._stream_factory = stream_factory
        self._frame_samples = int(sample_rate * frame_ms / 1000)

        if segmenter_factory is not None:
            self._segmenter = segmenter_factory()
        else:
            from .vad import VadSegmenter

            vad_cfg = vad_config
            preprocess = getattr(vad_cfg, "preprocess", None) if vad_cfg else None
            vad = getattr(vad_cfg, "vad", None) if vad_cfg else None

            self._segmenter = VadSegmenter(
                sample_rate=sample_rate,
                frame_ms=frame_ms,
                aggressiveness=getattr(vad, "aggressiveness", 2) if vad else 2,
                min_silence_ms=getattr(vad, "min_silence_ms", 500) if vad else 500,
                speech_pad_ms=getattr(vad, "speech_pad_ms", 150) if vad else 150,
                min_speech_ms=getattr(vad, "min_speech_ms", 200) if vad else 200,
                max_speech_ms=getattr(vad, "max_speech_ms", 30000) if vad else 30000,
                gate=getattr(preprocess, "gate", True) if preprocess else True,
                gate_threshold=getattr(preprocess, "gate_threshold", 200) if preprocess else 200,
            )

        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._stop = False
        self._paused = threading.Event()
        self._reset_pending = threading.Event()
        self._discard_tail = False

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        if not self._paused.is_set():
            self._paused.set()
            self._reset_pending.set()
            self._discard_tail = True

    def resume(self) -> None:
        self._paused.clear()
        self._discard_tail = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        if self._stream_factory is not None:
            self._stream = self._stream_factory(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
                device=self._device,
                blocksize=self._frame_samples,
            )
        else:
            import sounddevice as sd

            self._stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
                device=self._device,
                blocksize=self._frame_samples,
            )

        self._stream.start()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop:
            return
        self._stop = True

        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                try:
                    self._stream.stop()
                except Exception:
                    pass

        if self._thread is not None:
            self._thread.join(timeout=2.0)

        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        self._thread = None

    def _emit_segment(self, seg: bytes) -> None:
        if self._loop and self._on_segment:
            self._loop.call_soon_threadsafe(self._on_segment, seg)

    def _log(self, msg: str) -> None:
        if self._loop and self._on_log:
            self._loop.call_soon_threadsafe(self._on_log, msg)

    def _run(self) -> None:
        try:
            while not self._stop:
                try:
                    data, overflowed = self._stream.read(self._frame_samples)
                except Exception:
                    break
                if overflowed:
                    self._log("[mic] 入力オーバーフロー")
                if self._paused.is_set():
                    if self._reset_pending.is_set():
                        self._segmenter.reset()
                        self._reset_pending.clear()
                    continue
                pcm = _to_int16_mono_bytes(data, self._channels)
                for seg in self._segmenter.feed(pcm):
                    self._emit_segment(seg)
            if not self._discard_tail:
                tail = self._segmenter.flush()
                if tail:
                    self._emit_segment(tail)
        except Exception:
            pass
