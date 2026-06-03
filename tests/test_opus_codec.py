"""test_opus_codec: OpusStreamEncoder/Decoder のユニットテスト。

実 opuslib + soxr 往復テストと fake 注入によるフレーミング境界テスト。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from text_to_shaberi.common.opus_codec import (
    OPUS_CODEC_NAME,
    OPUS_FRAME_SIZE,
    OPUS_RATE,
    OpusStreamDecoder,
    OpusStreamEncoder,
    opus_available,
    opus_wire_format,
)


def _make_sine_pcm(
    freq: int = 440, duration_s: float = 1.0, sample_rate: int = 32000
) -> bytes:
    n = int(sample_rate * duration_s)
    t = np.arange(n, dtype=np.float64) / sample_rate
    wave = (np.sin(2 * np.pi * freq * t) * 16000).astype("<i2")
    return wave.tobytes()


@pytest.mark.skipif(not opus_available(), reason="opuslib/libopus not available")
class TestOpusRoundtrip:
    def test_32k_mono_1s_roundtrip(self) -> None:
        pcm = _make_sine_pcm(440, 1.0, 32000)
        enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
        packets = enc.feed(pcm) + enc.flush()

        assert len(packets) > 0
        for pkt in packets:
            assert len(pkt) > 0

        dec = OpusStreamDecoder()
        total_samples = 0
        for pkt in packets:
            decoded = dec.decode(pkt)
            assert len(decoded) == OPUS_FRAME_SIZE * 2
            total_samples += len(decoded) // 2

        assert 48000 <= total_samples <= 48000 + OPUS_FRAME_SIZE

    def test_each_packet_decodes_to_960_samples(self) -> None:
        pcm = _make_sine_pcm(440, 0.5, 32000)
        enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
        packets = enc.feed(pcm) + enc.flush()

        dec = OpusStreamDecoder()
        for pkt in packets:
            decoded = dec.decode(pkt)
            assert len(decoded) == OPUS_FRAME_SIZE * 2

    def test_16k_roundtrip(self) -> None:
        pcm = _make_sine_pcm(440, 0.5, 16000)
        enc = OpusStreamEncoder(src_rate=16000, bitrate=24000)
        packets = enc.feed(pcm) + enc.flush()
        assert len(packets) > 0

        dec = OpusStreamDecoder()
        total = sum(len(dec.decode(pkt)) // 2 for pkt in packets)
        assert 24000 <= total <= 24000 + OPUS_FRAME_SIZE

    def test_48k_passthrough_roundtrip(self) -> None:
        pcm = _make_sine_pcm(440, 0.5, 48000)
        enc = OpusStreamEncoder(src_rate=48000, bitrate=24000)
        packets = enc.feed(pcm) + enc.flush()
        assert len(packets) > 0

        dec = OpusStreamDecoder()
        total = sum(len(dec.decode(pkt)) // 2 for pkt in packets)
        assert 24000 <= total <= 24000 + OPUS_FRAME_SIZE

    def test_odd_byte_length_input(self) -> None:
        pcm = _make_sine_pcm(440, 0.3, 32000)
        odd = pcm[:-1]
        assert len(odd) % 2 == 1

        enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
        packets = enc.feed(odd) + enc.flush()
        assert len(packets) > 0

        dec = OpusStreamDecoder()
        total = sum(len(dec.decode(pkt)) // 2 for pkt in packets)
        assert total > 0

    def test_feed_small_chunks(self) -> None:
        pcm = _make_sine_pcm(440, 1.0, 32000)
        enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
        all_packets: list[bytes] = []
        chunk_size = 37
        for i in range(0, len(pcm), chunk_size):
            all_packets.extend(enc.feed(pcm[i : i + chunk_size]))
        all_packets.extend(enc.flush())

        assert len(all_packets) > 0
        dec = OpusStreamDecoder()
        total = sum(len(dec.decode(pkt)) // 2 for pkt in all_packets)
        assert 48000 <= total <= 48000 + OPUS_FRAME_SIZE

    def test_flush_returns_0_or_1_packets(self) -> None:
        pcm = _make_sine_pcm(440, 1.0, 32000)
        enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
        enc.feed(pcm)
        flush_pkts = enc.flush()
        assert len(flush_pkts) in (0, 1)

    def test_flush_empty_input(self) -> None:
        enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
        flush_pkts = enc.flush()
        assert len(flush_pkts) == 0

    def test_packets_nonempty(self) -> None:
        pcm = _make_sine_pcm(440, 1.0, 32000)
        enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
        packets = enc.feed(pcm) + enc.flush()
        for pkt in packets:
            assert len(pkt) > 0


class TestOpusWireFormat:
    def test_opus_wire_format_values(self) -> None:
        fmt = opus_wire_format()
        assert fmt.codec == OPUS_CODEC_NAME
        assert fmt.sample_rate == 48000
        assert fmt.channels == 1


class TestFakeEncoderFraming:
    """Fake encoder/decoder でフレーミング境界をテスト（実 opuslib 不要）。"""

    def test_feed_produces_complete_frames(self) -> None:
        recorded: list[bytes] = []

        def fake_encoder_factory():
            class FakeEncoder:
                def encode(self, data: bytes, frame_size: int) -> bytes:
                    assert len(data) == frame_size * 2
                    recorded.append(data)
                    return b"\x00" * 10
            return FakeEncoder()

        enc = OpusStreamEncoder(
            src_rate=48000, bitrate=24000, encoder_factory=fake_encoder_factory
        )
        pcm_2_frames = b"\x00\x00" * (OPUS_FRAME_SIZE * 2)
        pkts = enc.feed(pcm_2_frames)
        assert len(pkts) == 2
        assert len(recorded) == 2

    def test_carry_and_remainder(self) -> None:
        recorded: list[bytes] = []

        def fake_encoder_factory():
            class FakeEncoder:
                def encode(self, data: bytes, frame_size: int) -> bytes:
                    recorded.append(data)
                    return b"\x00" * 10
            return FakeEncoder()

        enc = OpusStreamEncoder(
            src_rate=48000, bitrate=24000, encoder_factory=fake_encoder_factory
        )
        frame_bytes = OPUS_FRAME_SIZE * 2
        half = b"\x00\x00" * (OPUS_FRAME_SIZE // 2)

        pkts1 = enc.feed(half)
        assert len(pkts1) == 0

        pkts2 = enc.feed(half)
        assert len(pkts2) == 1
        assert len(recorded) == 1

    def test_odd_byte_input_carry(self) -> None:
        recorded: list[bytes] = []

        def fake_encoder_factory():
            class FakeEncoder:
                def encode(self, data: bytes, frame_size: int) -> bytes:
                    recorded.append(data)
                    return b"\x00" * 10
            return FakeEncoder()

        enc = OpusStreamEncoder(
            src_rate=48000, bitrate=24000, encoder_factory=fake_encoder_factory
        )
        odd_input = b"\x00\x00" * 100 + b"\x00"
        pkts = enc.feed(odd_input)
        assert len(pkts) == 0

        even_input = b"\x00\x00"
        pkts2 = enc.feed(even_input)
        assert len(pkts2) == 0

    def test_resampler_factory_called(self) -> None:
        factory_called = [False]
        recorded_resampled: list[np.ndarray] = []

        def fake_resampler_factory():
            factory_called[0] = True

            class FakeRS:
                def resample_chunk(self, chunk, last=False):
                    recorded_resampled.append(chunk)
                    return chunk
            return FakeRS()

        def fake_encoder_factory():
            class FakeEncoder:
                def encode(self, data, frame_size):
                    return b"\x00" * 10
            return FakeEncoder()

        enc = OpusStreamEncoder(
            src_rate=32000,
            bitrate=24000,
            encoder_factory=fake_encoder_factory,
            resampler_factory=fake_resampler_factory,
        )
        pcm = b"\x00\x00" * 960
        enc.feed(pcm)
        assert factory_called[0]
        assert len(recorded_resampled) > 0

    def test_flush_drains_resampler(self) -> None:
        drain_called = [False]

        def fake_encoder_factory():
            class FakeEncoder:
                def encode(self, data, frame_size):
                    return b"\x00" * 10
            return FakeEncoder()

        def fake_resampler_factory():
            class FakeRS:
                def resample_chunk(self, chunk, last=False):
                    if last:
                        drain_called[0] = True
                        return np.zeros(OPUS_FRAME_SIZE, dtype=np.float32)
                    return chunk
            return FakeRS()

        enc = OpusStreamEncoder(
            src_rate=32000,
            bitrate=24000,
            encoder_factory=fake_encoder_factory,
            resampler_factory=fake_resampler_factory,
        )
        pcm = b"\x00\x00" * 480
        enc.feed(pcm)
        pkts = enc.flush()
        assert drain_called[0]
        # feed 480 + drain 960 = 1440 samples = 1 full frame(960) + padded partial(480→960)
        # = 2 packets。flush は encode に常に正確に OPUS_FRAME_SIZE を渡す（負パディング禁止）。
        assert len(pkts) == 2


class TestFakeDecoder:
    def test_fake_decoder(self) -> None:
        def fake_decoder_factory():
            class FakeDecoder:
                def decode(self, packet, frame_size):
                    return b"\x00\x00" * frame_size
            return FakeDecoder()

        dec = OpusStreamDecoder(decoder_factory=fake_decoder_factory)
        result = dec.decode(b"\xff" * 10)
        assert len(result) == OPUS_FRAME_SIZE * 2
