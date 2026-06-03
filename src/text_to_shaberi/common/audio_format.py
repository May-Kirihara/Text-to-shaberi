"""音声フォーマット定義（AudioFormat / CodecFormat）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AudioFormat:
    encoding: str = "pcm_s16le"
    sample_rate: int = 32000
    channels: int = 1


@dataclass(slots=True)
class CodecFormat:
    """X1 Opus 用の器。R0 では PCM 既定のみ。"""
    codec: str = "pcm_s16le"
    codec_rate: int | None = None
    frame_ms: int | None = None
