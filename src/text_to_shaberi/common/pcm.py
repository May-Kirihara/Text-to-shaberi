"""PCM 変換ヘルパー（_pcm_to_float32 / bytes_per_sample / split_full_samples / AudioBuffer）。

旧 tts/client.py から抽出。server TTS と client 再生で共有する。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def bytes_per_sample(encoding: str) -> int:
    """PCM encoding 名 → 1サンプルあたりのバイト数。"""
    match encoding:
        case "pcm_s16le":
            return 2
        case "pcm_s32le" | "pcm_f32le":
            return 4
    raise ValueError(f"unsupported encoding: {encoding}")


def split_full_samples(buf: bytes, frame_bytes: int) -> tuple[bytes, bytes]:
    """buf を完全フレーム境界で分割 → (完全フレーム分, 端数carry)。"""
    if frame_bytes <= 0:
        raise ValueError(f"frame_bytes must be positive: {frame_bytes}")
    remainder = len(buf) % frame_bytes
    if remainder == 0:
        return buf, b""
    cut = len(buf) - remainder
    return buf[:cut], buf[cut:]


def _pcm_to_float32(data: bytes, encoding: str, channels: int) -> np.ndarray:
    """生 PCM bytes → float32 ndarray（mono=1D, 多ch=2D）。"""
    match encoding:
        case "pcm_s16le":
            arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        case "pcm_s32le":
            arr = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
        case "pcm_f32le":
            arr = np.frombuffer(data, dtype="<f4").astype(np.float32)
        case _:
            raise ValueError(f"unsupported encoding: {encoding}")
    if channels > 1:
        return arr.reshape(-1, channels)
    return arr


@dataclass(slots=True)
class AudioBuffer:
    samples: np.ndarray  # mono=1D(n,), 多ch=2D(frames,channels), float32 [-1,1]
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate if self.sample_rate else 0.0
