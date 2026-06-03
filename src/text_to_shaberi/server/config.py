"""サーバ設定 (server.toml) のロード。"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.config import _only, load_toml

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BindConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""
    tls_cert: str = ""
    tls_key: str = ""


@dataclass(slots=True)
class QwenLocalConfig:
    model: str = "Qwen/Qwen3-ASR-1.7B"
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    attn_implementation: str = "sdpa"


@dataclass(slots=True)
class STTConfig:
    backend: str = "qwen_local"
    language: str = "ja"
    qwen_local: QwenLocalConfig = field(default_factory=QwenLocalConfig)


@dataclass(slots=True)
class TTSConfig:
    profiles_dir: str = ""
    active_profile: str = "gpt-sovits"


@dataclass(slots=True)
class TransportConfig:
    codec: str = "opus"
    opus_bitrate: int = 24000


@dataclass(slots=True)
class ServerConfig:
    bind: BindConfig = field(default_factory=BindConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)


def validate_bind(cfg: BindConfig) -> None:
    """host が loopback 以外で token 空なら ValueError。"""
    if cfg.token:
        return
    host = cfg.host
    if host in ("localhost", "::1"):
        return
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            f"bind.host '{host}' は解決不能な hostname です。"
            " 外部公開時は token を設定してください。"
        )
    if addr.is_loopback:
        return
    raise ValueError(
        f"bind.host '{host}' は loopback ではありません。"
        " 外部公開時は bind.token を設定してください。"
    )


def validate_tls(cfg: BindConfig) -> None:
    """tls_cert/tls_key は両方指定 or 両方空。指定時はファイル存在確認。"""
    cert = cfg.tls_cert
    key = cfg.tls_key
    if cert and not key:
        raise ValueError("bind.tls_cert が指定されていますが bind.tls_key がありません。")
    if key and not cert:
        raise ValueError("bind.tls_key が指定されていますが bind.tls_cert がありません。")
    if cert and key:
        if not Path(cert).is_file():
            raise ValueError(f"bind.tls_cert ファイルが存在しません: {cert}")
        if not Path(key).is_file():
            raise ValueError(f"bind.tls_key ファイルが存在しません: {key}")


def normalize_transport_codec(s: str) -> str:
    """設定値 → wire 名正規化。"""
    from ..common.opus_codec import OPUS_CODEC_NAME

    if s == "opus":
        return OPUS_CODEC_NAME
    if s in ("pcm", "pcm_s16le"):
        return "pcm_s16le"
    logger.warning("未知の codec 設定 '%s' → pcm_s16le にフォールバック", s)
    return "pcm_s16le"


def load_server_config(path: str | Any) -> ServerConfig:
    raw = load_toml(path)
    bind_raw = raw.get("bind", {})
    stt_raw = raw.get("stt", {})
    tts_raw = raw.get("tts", {})
    transport_raw = raw.get("transport", {})
    return ServerConfig(
        bind=BindConfig(**_only(bind_raw, BindConfig)),
        stt=STTConfig(
            backend=stt_raw.get("backend", "qwen_local"),
            language=stt_raw.get("language", "ja"),
            qwen_local=QwenLocalConfig(**_only(stt_raw.get("qwen_local", {}), QwenLocalConfig)),
        ),
        tts=TTSConfig(
            profiles_dir=tts_raw.get("profiles_dir", ""),
            active_profile=tts_raw.get("active_profile", "gpt-sovits"),
        ),
        transport=TransportConfig(**_only(transport_raw, TransportConfig)),
    )
