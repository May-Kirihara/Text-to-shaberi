"""toml駆動の汎用HTTP TTSドライバ（M2: 同期取得＋ストリーミング受信）。

プロファイルの request 仕様で TTS-API を叩き、response.mode と audio フォーマットに
従って音声を numpy float32 [-1,1] へデコードする。M1 はレスポンス全体を受け取ってから
返す。M2 でチャンク逐次受信（stream）を追加し、TTFA を短縮する。
"""
from __future__ import annotations

import base64
import io
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from .profile import AudioFormat, TTSProfile


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


def _decode_raw(data: bytes, fmt: AudioFormat) -> AudioBuffer:
    samples = _pcm_to_float32(data, fmt.encoding, fmt.channels)
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

    def _build_request_kwargs(
        self, profile: TTSProfile, body: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        """(method, url, kwargs) を返す。body種別に応じて params/json/data を切替。"""
        kwargs: dict[str, Any] = {"headers": profile.headers, "timeout": profile.timeout_sec}
        if profile.method.upper() == "GET":
            kwargs["params"] = body
        elif profile.body_type == "json":
            kwargs["json"] = body
        elif profile.body_type in ("form", "multipart"):
            # multipartのファイル送信はM2では未対応
            kwargs["data"] = body
        else:
            kwargs["json"] = body
        return profile.method, profile.url, kwargs

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
        method, url, kwargs = self._build_request_kwargs(profile, body)
        resp = await self._client.request(method, url, **kwargs)
        resp.raise_for_status()
        return self._decode(profile, resp)

    async def stream(
        self, profile: TTSProfile, text: str, **variables: Any
    ) -> AsyncIterator[bytes]:
        """プロファイルに従い HTTP ストリーミング受信 → バイトチャンクを逐次 yield。

        呼び出し側は ``profile.response_mode == "audio_stream"`` を判定してから使うこと。
        Task.cancel() 時は async with が HTTP 接続を確実に閉じる。
        """
        body = profile.render_body(text, **variables)
        method, url, kwargs = self._build_request_kwargs(profile, body)
        async with self._client.stream(method, url, **kwargs) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk


def _extract(payload: dict, profile: TTSProfile) -> str:
    """response.extract.path（ドット区切り）で JSON からフィールドを取り出す（json系用）。"""
    # M1ではプロファイルにextract定義が無いので素朴に "audio" を探す。M6で schema 化。
    if "audio" in payload:
        return payload["audio"]
    raise ValueError("json_base64 のフィールド抽出に失敗（extract.path 未対応）")
