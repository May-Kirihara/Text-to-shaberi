"""クライアント設定 (client.toml) のロード。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..common.config import _only, load_toml


@dataclass(slots=True)
class ServerConfig:
    address: str = "ws://127.0.0.1:8765/ws"
    token: str = ""


@dataclass(slots=True)
class OutputConfig:
    device: str = "default"
    resample: str = "auto"


@dataclass(slots=True)
class VADConfig:
    engine: str = "webrtc"
    min_silence_ms: int = 500
    speech_pad_ms: int = 150
    aggressiveness: int = 2
    frame_ms: int = 30
    min_speech_ms: int = 200
    max_speech_ms: int = 30000


@dataclass(slots=True)
class MicPreprocessConfig:
    gate: bool = True
    denoise: str = "off"
    gate_threshold: int = 200


@dataclass(slots=True)
class MicConfig:
    device: str = "default"
    sample_rate: int = 16000
    channels: int = 1
    preprocess: MicPreprocessConfig = field(default_factory=MicPreprocessConfig)
    vad: VADConfig = field(default_factory=VADConfig)


@dataclass(slots=True)
class UIConfig:
    input_method: str = "ibus"


@dataclass(slots=True)
class ProfileConfig:
    active_id: str = ""


@dataclass(slots=True)
class ClientConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    mic: MicConfig = field(default_factory=MicConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)


def load_client_config(path: str | Any) -> ClientConfig:
    raw = load_toml(path)
    mic_raw = raw.get("mic") or {}
    preprocess_raw = mic_raw.get("preprocess") or {}
    vad_raw = mic_raw.get("vad") or {}
    return ClientConfig(
        server=ServerConfig(**_only(raw.get("server") or {}, ServerConfig)),
        output=OutputConfig(**_only(raw.get("output") or {}, OutputConfig)),
        mic=MicConfig(
            device=mic_raw.get("device", "default"),
            sample_rate=int(mic_raw.get("sample_rate", 16000)),
            channels=int(mic_raw.get("channels", 1)),
            preprocess=MicPreprocessConfig(
                **_only(preprocess_raw, MicPreprocessConfig)
            ),
            vad=VADConfig(**_only(vad_raw, VADConfig)),
        ),
        ui=UIConfig(**_only(raw.get("ui") or {}, UIConfig)),
        profile=ProfileConfig(**_only(raw.get("profile") or {}, ProfileConfig)),
    )
