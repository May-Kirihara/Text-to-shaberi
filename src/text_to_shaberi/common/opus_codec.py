"""Opus コーデックストリーミングエンコーダ/デコーダ（X1 §3.1）。

opuslib / soxr は遅延 import（関数内/factory 内）。
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from .pcm import _pcm_to_float32, bytes_per_sample, split_full_samples
from .protocol import AudioFormatPayload

logger = logging.getLogger(__name__)

OPUS_CODEC_NAME = "opus"
OPUS_RATE = 48000
OPUS_FRAME_MS = 20
OPUS_FRAME_SIZE = 960
OPUS_CHANNELS = 1


def opus_wire_format() -> AudioFormatPayload:
    return AudioFormatPayload(
        codec=OPUS_CODEC_NAME, sample_rate=OPUS_RATE, channels=OPUS_CHANNELS
    )


def opus_available() -> bool:
    try:
        import opuslib

        opuslib.Encoder(OPUS_RATE, OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
        return True
    except Exception:
        return False


class OpusStreamEncoder:
    def __init__(
        self,
        src_rate: int,
        bitrate: int = 24000,
        *,
        encoder_factory: Callable[[], object] | None = None,
        resampler_factory: Callable[[], object] | None = None,
    ) -> None:
        self._src_rate = src_rate
        self._bitrate = bitrate
        self._buf = b""
        self._input_carry = b""

        if encoder_factory is not None:
            self._encoder = encoder_factory()
        else:
            import opuslib

            enc = opuslib.Encoder(OPUS_RATE, OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
            enc.bitrate = bitrate
            self._encoder = enc

        if src_rate != OPUS_RATE:
            if resampler_factory is not None:
                self._rs = resampler_factory()
            else:
                import soxr

                self._rs = soxr.ResampleStream(
                    src_rate, OPUS_RATE, OPUS_CHANNELS, dtype="float32", quality="HQ"
                )
        else:
            self._rs = None

    def feed(self, pcm_s16le_mono: bytes) -> list[bytes]:
        full, self._input_carry = split_full_samples(
            self._input_carry + pcm_s16le_mono,
            bytes_per_sample("pcm_s16le") * OPUS_CHANNELS,
        )
        if not full:
            return []

        frames = _pcm_to_float32(full, "pcm_s16le", OPUS_CHANNELS)
        if self._rs is not None:
            resampled = self._rs.resample_chunk(frames)
        else:
            resampled = frames

        if resampled.size == 0:
            return []

        pcm48 = (np.clip(resampled, -1, 1) * 32767).astype("<i2").tobytes()
        self._buf += pcm48

        packets: list[bytes] = []
        frame_bytes = OPUS_FRAME_SIZE * 2
        while len(self._buf) >= frame_bytes:
            chunk = self._buf[:frame_bytes]
            self._buf = self._buf[frame_bytes:]
            pkt = self._encoder.encode(chunk, OPUS_FRAME_SIZE)
            packets.append(pkt)

        return packets

    def flush(self) -> list[bytes]:
        if self._rs is not None:
            drained = self._rs.resample_chunk(
                np.empty((0,), dtype=np.float32), last=True
            )
            if drained.size > 0:
                pcm48 = (np.clip(drained, -1, 1) * 32767).astype("<i2").tobytes()
                self._buf += pcm48

        if not self._buf:
            return []

        frame_bytes = OPUS_FRAME_SIZE * 2
        packets: list[bytes] = []
        # drain で残差が 1 フレーム超になり得るため、まず完全フレームを吐く
        while len(self._buf) >= frame_bytes:
            chunk = self._buf[:frame_bytes]
            self._buf = self._buf[frame_bytes:]
            packets.append(self._encoder.encode(chunk, OPUS_FRAME_SIZE))
        # 残りの端数のみ無音パディングして最後の 1 パケット
        if self._buf:
            padded = self._buf + b"\x00" * (frame_bytes - len(self._buf))
            packets.append(self._encoder.encode(padded, OPUS_FRAME_SIZE))
        self._buf = b""
        return packets


class OpusStreamDecoder:
    def __init__(
        self, *, decoder_factory: Callable[[], object] | None = None
    ) -> None:
        if decoder_factory is not None:
            self._decoder = decoder_factory()
        else:
            import opuslib

            self._decoder = opuslib.Decoder(OPUS_RATE, OPUS_CHANNELS)

    def decode(self, packet: bytes) -> bytes:
        return self._decoder.decode(packet, OPUS_FRAME_SIZE)
