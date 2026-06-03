"""音声再生（M2: ブロッキング再生＋ストリーミング逐次再生）。

出力デバイスは TTS の出力rate（例 32000Hz）を受け付けないことがある（生のALSA hwデバイス等は
44100/48000固定）。そのため**出力デバイスの既定rateへ soxr で変換**してから再生する。
M2 で OutputStream を使ったチャンク逐次再生（StreamingPlayer）を追加。
C1: 遅延スタート・threading.Lock・request_abort/close 分離・factory 注入 seam。
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

from ...common.pcm import _pcm_to_float32, bytes_per_sample, split_full_samples

logger = logging.getLogger(__name__)


def flush_resampler(rs: Any, channels: int = 1) -> np.ndarray | None:
    if rs is None:
        return None
    empty = (
        np.empty((0,), dtype=np.float32)
        if channels == 1
        else np.empty((0, channels), dtype=np.float32)
    )
    return rs.resample_chunk(empty, last=True)


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
        # または player.request_abort()  # 即停止（mainスレッドから）
        # 続けて worker スレッドで player.close()  # デバイス解放
    """

    def __init__(
        self,
        src_rate: int,
        channels: int = 1,
        device: int | None = None,
        encoding: str = "pcm_s16le",
        preroll_ms: int = 80,
        prime_ms: int = 500,
        *,
        stream_factory=None,
        resampler_factory=None,
        target_sr: int | None = None,
    ) -> None:
        self._encoding = encoding
        self._channels = channels
        self._frame_bytes = bytes_per_sample(encoding) * channels
        self._prime_ms = prime_ms
        self._primed = False

        if target_sr is None:
            import sounddevice as sd

            if device is None:
                from .devices import preferred_default_output

                device = preferred_default_output()
            target_sr = _device_samplerate(sd, device)

        self._target_sr = target_sr

        self._resampler = None
        if self._target_sr != src_rate:
            if resampler_factory is not None:
                self._resampler = resampler_factory(src_rate, self._target_sr, channels)
            else:
                import soxr

                self._resampler = soxr.ResampleStream(
                    src_rate, self._target_sr, channels, dtype="float32", quality="HQ"
                )

        self._src_rate = src_rate
        if stream_factory is not None:
            self._stream = stream_factory(
                samplerate=self._target_sr,
                channels=channels,
                dtype="float32",
                device=device,
                latency="low",
            )
        else:
            import sounddevice as sd

            self._stream = sd.OutputStream(
                samplerate=self._target_sr,
                channels=channels,
                dtype="float32",
                device=device,
                latency="low",
            )

        self._lock = threading.Lock()
        self._aborted = False
        self._closed = False
        self._started_stream = False

        self._carry = b""
        self._preroll: list[np.ndarray] = []
        self._preroll_frames = int(self._target_sr * preroll_ms / 1000)
        self._started_audio = False

    def _ensure_started(self) -> None:
        if not self._started_stream:
            self._stream.start()
            self._started_stream = True
            # デバイス/PipeWire の起動ウォームアップで先頭音が食われるのを防ぐため、
            # 開始直後に無音を prime して食い込み窓を無音で吸収する（先頭欠け対策）。
            if not self._primed:
                self._primed = True
                if self._prime_ms > 0:
                    n = int(self._target_sr * self._prime_ms / 1000)
                    if n > 0:
                        if self._channels == 1:
                            silence = np.zeros(n, dtype=np.float32)
                        else:
                            silence = np.zeros((n, self._channels), dtype=np.float32)
                        try:
                            self._stream.write(silence)
                        except Exception:
                            pass

    def write_bytes(self, data: bytes) -> None:
        with self._lock:
            if self._aborted or self._closed:
                return

        full, self._carry = split_full_samples(self._carry + data, self._frame_bytes)
        if not full:
            return

        frames = _pcm_to_float32(full, self._encoding, self._channels)

        if self._resampler is not None:
            frames = self._resampler.resample_chunk(frames)
        if len(frames) == 0:
            return

        with self._lock:
            if self._aborted or self._closed:
                return

            if not self._started_audio:
                self._preroll.append(frames)
                total = sum(len(f) for f in self._preroll)
                if total >= self._preroll_frames:
                    combined = np.concatenate(self._preroll)
                    self._preroll.clear()
                    self._ensure_started()
                    try:
                        self._stream.write(combined)
                    except Exception:
                        self._aborted = True
                        return
                    self._started_audio = True
            else:
                try:
                    self._stream.write(frames)
                except Exception:
                    self._aborted = True
                    return

    def request_abort(self) -> None:
        with self._lock:
            if self._aborted:
                return
            self._aborted = True
            if self._started_stream:
                try:
                    self._stream.abort()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

            tail: np.ndarray | None = None
            if self._resampler is not None:
                tail = flush_resampler(self._resampler, self._channels)

            chunks: list[np.ndarray] = []
            if self._preroll:
                chunks.extend(self._preroll)
                self._preroll.clear()
            if tail is not None and len(tail) > 0:
                chunks.append(tail)

            if self._aborted:
                if self._started_stream:
                    try:
                        self._stream.stop()
                    except Exception:
                        pass
                try:
                    self._stream.close()
                except Exception:
                    pass
                return

            if chunks:
                self._ensure_started()
                try:
                    self._stream.write(np.concatenate(chunks))
                except Exception:
                    pass

            if self._started_stream:
                time.sleep(float(self._stream.latency) + 0.05)
                try:
                    self._stream.stop()
                except Exception:
                    pass
            try:
                self._stream.close()
            except Exception:
                pass
