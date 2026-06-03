"""toml駆動の汎用HTTP TTSドライバ（M1: 同期取得）。

プロファイルの request 仕様で TTS-API を叩き、response.mode と audio フォーマットに
従って音声を numpy float32 [-1,1] へデコードする。M1 はレスポンス全体を受け取ってから
返す（チャンク逐次再生は M2）。
"""
from __future__ import annotations

import base64
import io
import wave
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from .profile import AudioFormat, TTSProfile

# encoding 名 → numpy dtype（raw/PCM 用）
_PCM_DTYPE: dict[str, str] = {
    "pcm_s16le": "<i2",
    "pcm_s24le": "<i4",  # 近似（24bitは後続で厳密対応）
    "pcm_s32le": "<i4",
    "pcm_f32le": "<f4",
}
_PCM_FULLSCALE: dict[str, float] = {
    "<i2": 32768.0,
    "<i4": 2147483648.0,
}


@dataclass(slots=True)
class AudioBuffer:
    samples: np.ndarray  # shape (frames, channels), float32 [-1,1]
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate if self.sample_rate else 0.0


def _decode_raw(data: bytes, fmt: AudioFormat) -> AudioBuffer:
    dtype = _PCM_DTYPE.get(fmt.encoding, "<i2")
    arr = np.frombuffer(data, dtype=dtype)
    if dtype.endswith("f4"):
        samples = arr.astype(np.float32)
    else:
        samples = arr.astype(np.float32) / _PCM_FULLSCALE.get(dtype, 32768.0)
    if fmt.channels > 1:
        samples = samples.reshape(-1, fmt.channels)
    return AudioBuffer(samples=samples, sample_rate=fmt.sample_rate)


def _decode_wav(data: bytes) -> AudioBuffer:
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if width == 2:
        arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        arr = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        arr = (np.frombuffer(frames, dtype="u1").astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"未対応のwav sample width: {width}")
    if channels > 1:
        arr = arr.reshape(-1, channels)
    return AudioBuffer(samples=arr, sample_rate=rate)


class TTSClient:
    """1つの httpx.AsyncClient を keep-alive で再利用する。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_request(self, profile: TTSProfile, body: dict[str, Any]) -> httpx.Request:
        kwargs: dict[str, Any] = {"headers": profile.headers, "timeout": profile.timeout_sec}
        if profile.method.upper() == "GET":
            kwargs["params"] = body
        elif profile.body_type == "json":
            kwargs["json"] = body
        elif profile.body_type in ("form", "multipart"):
            kwargs["data"] = body
        else:  # raw_template 等は将来対応
            kwargs["json"] = body
        return self._client.build_request(profile.method, profile.url, **kwargs)

    def _decode(self, profile: TTSProfile, resp: httpx.Response) -> AudioBuffer:
        data = resp.content
        content_type = resp.headers.get("content-type", "").lower()
        mode = profile.response_mode

        if mode in ("audio_stream", "audio_file"):
            if "wav" in content_type or data[:4] == b"RIFF":
                return _decode_wav(data)
            return _decode_raw(data, profile.audio)
        if mode == "json_base64":
            payload = resp.json()
            field = _extract(payload, profile)
            audio_bytes = base64.b64decode(field)
            if audio_bytes[:4] == b"RIFF":
                return _decode_wav(audio_bytes)
            return _decode_raw(audio_bytes, profile.audio)
        raise ValueError(f"未対応の response.mode: {mode}")

    async def synthesize(self, profile: TTSProfile, text: str, **variables: Any) -> AudioBuffer:
        body = profile.render_body(text, **variables)
        request = self._build_request(profile, body)
        resp = await self._client.send(request)
        resp.raise_for_status()
        return self._decode(profile, resp)


def _extract(payload: dict, profile: TTSProfile) -> str:
    """response.extract.path（ドット区切り）で JSON からフィールドを取り出す（json系用）。"""
    # M1ではプロファイルにextract定義が無いので素朴に "audio" を探す。M6で schema 化。
    if "audio" in payload:
        return payload["audio"]
    raise ValueError("json_base64 のフィールド抽出に失敗（extract.path 未対応）")
