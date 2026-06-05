"""クライアント設定 (client.toml) のロード。"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from ..common.config import _only, load_toml


@dataclass(slots=True)
class ServerConfig:
    address: str = "ws://127.0.0.1:8765/ws"
    token: str = ""
    codec: str = "opus"
    tls_insecure: bool = False
    tls_cafile: str = ""
    token_in_query: bool = False
    reconnect: bool = True
    reconnect_max_delay: float = 30.0


@dataclass(slots=True)
class OutputConfig:
    device: str = "default"
    resample: str = "auto"
    prime_ms: int = 0
    muted: bool = False  # true で起動時からローカル再生をミュート（monitor/forward_audio は継続）


@dataclass(slots=True)
class VADConfig:
    engine: str = "webrtc"
    min_silence_ms: int = 500
    speech_pad_ms: int = 150
    aggressiveness: int = 2
    frame_ms: int = 30
    min_speech_ms: int = 200
    max_speech_ms: int = 30000
    seg_silence_ms: int = 250
    end_silence_ms: int = 500
    seg_min_ms: int = 400
    seg_max_ms: int = 2500


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
    stream_mode: bool = False
    preprocess: MicPreprocessConfig = field(default_factory=MicPreprocessConfig)
    vad: VADConfig = field(default_factory=VADConfig)


@dataclass(slots=True)
class UIConfig:
    input_method: str = "ibus"


@dataclass(slots=True)
class ProfileConfig:
    active_id: str = ""


@dataclass(slots=True)
class InputConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8770
    token: str = ""
    forward_audio: bool = False
    seg_max_chars: int = 80
    seg_hard_max_chars: int = 160


@dataclass(slots=True)
class MonitorConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8771
    token: str = ""
    queue_max: int = 256


@dataclass(slots=True)
class ClientConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    mic: MicConfig = field(default_factory=MicConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    input: InputConfig = field(default_factory=InputConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)


def load_client_config(path: str | Any) -> ClientConfig:
    raw = load_toml(path)
    mic_raw = raw.get("mic") or {}
    preprocess_raw = mic_raw.get("preprocess") or {}
    vad_raw = mic_raw.get("vad") or {}
    input_raw = raw.get("input") or {}
    cfg = ClientConfig(
        server=ServerConfig(**_only(raw.get("server") or {}, ServerConfig)),
        output=OutputConfig(**_only(raw.get("output") or {}, OutputConfig)),
        mic=MicConfig(
            device=mic_raw.get("device", "default"),
            sample_rate=int(mic_raw.get("sample_rate", 16000)),
            channels=int(mic_raw.get("channels", 1)),
            stream_mode=bool(mic_raw.get("stream_mode", False)),
            preprocess=MicPreprocessConfig(
                **_only(preprocess_raw, MicPreprocessConfig)
            ),
            vad=VADConfig(**_only(vad_raw, VADConfig)),
        ),
        ui=UIConfig(**_only(raw.get("ui") or {}, UIConfig)),
        profile=ProfileConfig(**_only(raw.get("profile") or {}, ProfileConfig)),
        input=InputConfig(**_only(input_raw, InputConfig)),
        monitor=MonitorConfig(**_only(raw.get("monitor") or {}, MonitorConfig)),
    )

    vad = cfg.mic.vad
    clamped = False
    vad.seg_silence_ms = max(1, vad.seg_silence_ms)
    vad.end_silence_ms = max(1, vad.end_silence_ms)
    vad.seg_min_ms = max(1, vad.seg_min_ms)
    vad.seg_max_ms = max(1, vad.seg_max_ms)
    floor = vad.seg_silence_ms + 2 * vad.frame_ms
    if vad.end_silence_ms < floor:
        clamped = True
        vad.end_silence_ms = floor
    if vad.seg_max_ms < vad.seg_min_ms:
        clamped = True
        vad.seg_max_ms = vad.seg_min_ms
    if clamped:
        print(
            f"[config] VADパラメータをクランプしました: "
            f"end_silence_ms={vad.end_silence_ms}, seg_max_ms={vad.seg_max_ms}",
            file=sys.stderr,
        )

    inp = cfg.input
    if inp.host not in ("127.0.0.1", "::1", "localhost") and not inp.token:
        print(
            f"[config] input.host={inp.host} は非loopbackですが token 未設定 "
            f"→ 入力口を無効化します(enabled=False)",
            file=sys.stderr,
        )
        inp.enabled = False
    if inp.seg_hard_max_chars < inp.seg_max_chars:
        inp.seg_hard_max_chars = inp.seg_max_chars

    mon = cfg.monitor
    if mon.host not in ("127.0.0.1", "::1", "localhost") and not mon.token:
        print(
            f"[config] monitor.host={mon.host} は非loopbackですが token 未設定 "
            f"→ 音声モニタを無効化します(enabled=False)",
            file=sys.stderr,
        )
        mon.enabled = False
    if mon.queue_max < 1:
        mon.queue_max = 1
    return cfg
