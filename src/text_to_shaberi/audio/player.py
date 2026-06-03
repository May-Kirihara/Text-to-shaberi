"""音声再生（M1: ブロッキング再生＋出力rateへの soxr リサンプル）。

出力デバイスは TTS の出力rate（例 32000Hz）を受け付けないことがある（生のALSA hwデバイス等は
44100/48000固定）。そのため**出力デバイスの既定rateへ soxr で変換**してから再生する。
チャンク逐次再生（OutputStream）と明示的なデバイス切替(drain/stop/reopen)は M2 で拡張する。
"""
from __future__ import annotations

import numpy as np


def _device_samplerate(sd, device: int | None) -> int:
    info = sd.query_devices(kind="output") if device is None else sd.query_devices(device)
    return int(info["default_samplerate"])


def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    """samples(float32 [-1,1]) を device で再生し、完了まで待つ（ブロッキング）。

    出力デバイスの既定rateへ必要に応じてリサンプルする。別スレッド(executor)から呼ぶ前提。
    """
    import sounddevice as sd  # 遅延import（libportaudio2 が必要）

    if device is None:
        # PortAudioのdefaultは無音hwを指すことがあるため、PipeWire/pulse経由（=システム既定sink）を優先
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
