"""M2: split_full_samples / _pcm_to_float32 ユニットテスト（sounddevice 不要）。"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from text_to_shaberi.tts.client import (
    _pcm_to_float32,
    bytes_per_sample,
    split_full_samples,
)


class TestSplitFullSamples:
    def test_empty(self) -> None:
        full, carry = split_full_samples(b"", 2)
        assert full == b""
        assert carry == b""

    def test_exact_fit(self) -> None:
        full, carry = split_full_samples(b"\x01\x02\x03\x04", 2)
        assert full == b"\x01\x02\x03\x04"
        assert carry == b""

    def test_with_remainder(self) -> None:
        full, carry = split_full_samples(b"\x01\x02\x03", 2)
        assert full == b"\x01\x02"
        assert carry == b"\x03"

    def test_single_byte_remainder(self) -> None:
        full, carry = split_full_samples(b"\xAA", 2)
        assert full == b""
        assert carry == b"\xAA"

    def test_frame_bytes_4(self) -> None:
        data = b"\x01\x02\x03\x04\x05\x06\x07"
        full, carry = split_full_samples(data, 4)
        assert full == b"\x01\x02\x03\x04"
        assert carry == b"\x05\x06\x07"

    def test_invalid_frame_bytes(self) -> None:
        with pytest.raises(ValueError):
            split_full_samples(b"\x01", 0)


class TestBytesPerSample:
    def test_s16le(self) -> None:
        assert bytes_per_sample("pcm_s16le") == 2

    def test_s32le(self) -> None:
        assert bytes_per_sample("pcm_s32le") == 4

    def test_f32le(self) -> None:
        assert bytes_per_sample("pcm_f32le") == 4

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported encoding"):
            bytes_per_sample("pcm_s24le")


class TestPcmToFloat32:
    def test_s16le_mono_1d(self) -> None:
        raw = struct.pack("<hh", 0, 32767)
        out = _pcm_to_float32(raw, "pcm_s16le", 1)
        assert out.ndim == 1
        np.testing.assert_allclose(out, [0.0, 32767 / 32768], atol=1e-7)

    def test_s16le_2ch_2d(self) -> None:
        raw = struct.pack("<hhhh", 1000, -1000, 0, 32767)
        out = _pcm_to_float32(raw, "pcm_s16le", 2)
        assert out.shape == (2, 2)
        np.testing.assert_allclose(out[0], [1000 / 32768, -1000 / 32768], atol=1e-7)

    def test_s32le_mono(self) -> None:
        raw = struct.pack("<i", 2147483647)
        out = _pcm_to_float32(raw, "pcm_s32le", 1)
        np.testing.assert_allclose(out, [2147483647 / 2147483648], atol=1e-7)

    def test_f32le_mono_passthrough(self) -> None:
        raw = struct.pack("<ff", 0.5, -0.25)
        out = _pcm_to_float32(raw, "pcm_f32le", 1)
        assert out.ndim == 1
        np.testing.assert_allclose(out, [0.5, -0.25], atol=1e-7)

    def test_unknown_encoding_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported encoding"):
            _pcm_to_float32(b"\x00\x00", "pcm_s24le", 1)
