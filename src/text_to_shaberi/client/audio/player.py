"""音声再生（M2: ブロッキング再生＋ストリーミング逐次再生）。

出力デバイスは TTS の出力rate（例 32000Hz）を受け付けないことがある（生のALSA hwデバイス等は
44100/48000固定）。そのため**出力デバイスの既定rateへ soxr で変換**してから再生する。
M2 で OutputStream を使ったチャンク逐次再生（StreamingPlayer）を追加。
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ...common.pcm import _pcm_to_float32, bytes_per_sample, split_full_samples

logger = logging.getLogger(__name__)


def _device_samplerate(sd, device: int | None) -> int:
    info = sd.query_devices(kind="output") if device is None else sd.query_devices(device)
    return int(info["default_samplerate"])


def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    """samples(float32 [-1,1]) を device で再生し、完了まで待つ（ブロッキング）。

    出力デバイスの既定rateへ必要に応じてリサンプルする。別スレッド(executor)から呼ぶ前提。
    """
    import sounddevice as sd

    if device is None:
        from .devices import preferred_default_output

        device = preferred_default_output()

    target_sr = _device_samplerate(sd, device)
    data = samples
    if target_sr != sample_rate:
        import soxr

        data = soxr.resample(samples, sample_rate, target_sr)
    sd.play(data, samplerate=target_sr, device=device)
    sd.wait()


def stop() -> None:
    import sounddevice as sd

    sd.stop()


class StreamingPlayer:
    """OutputStream + soxr リサンプルでチャンク逐次再生。

    使い方:
        player = StreamingPlayer(src_rate, channels, device, encoding)
        for chunk in http_stream:
            player.write_bytes(chunk)
        player.close()   # 正常終了（末尾まで鳴らす）
        # または player.abort()  # 即停止
    """

    def __init__(
        self,
        src_rate: int,
        channels: int = 1,
        device: int | None = None,
        encoding: str = "pcm_s16le",
        preroll_ms: int = 40,
    ) -> None:
        import sounddevice as sd
        import soxr

        self._encoding = encoding
        self._channels = channels
        self._frame_bytes = bytes_per_sample(encoding) * channels

        if device is None:
            from .devices import preferred_default_output

            device = preferred_default_output()

        self._target_sr = _device_samplerate(sd, device)

        self._resampler: soxr.ResampleStream | None = None
        if self._target_sr != src_rate:
            self._resampler = soxr.ResampleStream(
                src_rate, self._target_sr, channels, dtype="float32", quality="HQ"
            )

        self._stream = sd.OutputStream(
            samplerate=self._target_sr,
            channels=channels,
            dtype="float32",
            device=device,
            latency="low",
        )
        self._stream.start()
        self._closed = False

        self._carry = b""
        self._preroll: list[np.ndarray] = []
        self._preroll_frames = int(self._target_sr * preroll_ms / 1000)
        self._started_audio = False

    def write_bytes(self, data: bytes) -> None:
        """PCM チャンクを受け取り、サンプル境界に整えてから OutputStream へ書き込む。"""
        full, self._carry = split_full_samples(self._carry + data, self._frame_bytes)
        if not full:
            return

        frames = _pcm_to_float32(full, self._encoding, self._channels)

        if self._resampler is not None:
            frames = self._resampler.resample_chunk(frames)
        if len(frames) == 0:
            return

        if not self._started_audio:
            self._preroll.append(frames)
            total = sum(len(f) for f in self._preroll)
            if total >= self._preroll_frames:
                combined = np.concatenate(self._preroll)
                self._preroll.clear()
                try:
                    self._stream.write(combined)
                except Exception:
                    self.abort()
                    raise
                self._started_audio = True
        else:
            try:
                self._stream.write(frames)
            except Exception:
                self.abort()
                raise

    def close(self) -> None:
        """正常終了。resampler 残差を flush し、tail drain 後に stop+close。"""
        if self._closed:
            return
        self._closed = True

        tail: np.ndarray | None = None
        if self._resampler is not None:
            empty = (
                np.empty((0,), dtype=np.float32)
                if self._channels == 1
                else np.empty((0, self._channels), dtype=np.float32)
            )
            tail = self._resampler.resample_chunk(empty, last=True)

        chunks: list[np.ndarray] = []
        if tail is not None and len(tail) > 0:
            chunks.append(tail)
        if self._preroll:
            chunks.extend(self._preroll)
            self._preroll.clear()

        if chunks:
            try:
                self._stream.write(np.concatenate(chunks))
            except Exception:
                pass

        time.sleep(float(self._stream.latency) + 0.05)
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    def abort(self) -> None:
        """即停止（idempotent）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.abort()
        except Exception:
            pass
        try:
            self._stream.close()
        except Exception:
            pass
