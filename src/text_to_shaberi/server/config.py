"""サーバ設定 (server.toml) のロード。"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from ..common.config import _only, load_toml


@dataclass(slots=True)
class BindConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""


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
    codec: str = "pcm"


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
