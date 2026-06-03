"""アプリ設定 (config.toml) のロード。

未知キーは無視し、欠損キーは既定値で補完する（前方/後方互換のため）。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VADConfig:
    engine: str = "silero"
    min_silence_ms: int = 500
    speech_pad_ms: int = 150


@dataclass(slots=True)
class MicPreprocessConfig:
    gate: bool = True
    denoise: str = "off"


@dataclass(slots=True)
class MicConfig:
    device: str = "default"
    sample_rate: int = 16000
    channels: int = 1
    preprocess: MicPreprocessConfig = field(default_factory=MicPreprocessConfig)
    vad: VADConfig = field(default_factory=VADConfig)


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
class OutputConfig:
    device: str = "default"
    resample: str = "auto"


@dataclass(slots=True)
class APIConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""


@dataclass(slots=True)
class UIConfig:
    # ibus | virtualkeyboard | system
    #  ibus           : アプリだけ ibus+mozc を使い実キーボードで日本語入力（既定）
    #  virtualkeyboard: PySide6同梱の画面キーボード
    #  system         : 環境変数を継承（X11+fcitx5は版差で不可なことに注意）
    input_method: str = "ibus"


@dataclass(slots=True)
class AppConfig:
    active_tts_profile: str = "gpt-sovits"
    mic: MicConfig = field(default_factory=MicConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    api: APIConfig = field(default_factory=APIConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    # 実行時に解決するパス
    config_path: Path | None = None
    profiles_dir: Path | None = None


def _only(d: dict[str, Any], cls: type) -> dict[str, Any]:
    """dataclass のフィールドに存在するキーだけ抽出（未知キーは無視）。"""
    valid = set(cls.__dataclass_fields__)
    return {k: v for k, v in d.items() if k in valid}


def _mic(d: dict[str, Any]) -> MicConfig:
    return MicConfig(
        device=d.get("device", "default"),
        sample_rate=int(d.get("sample_rate", 16000)),
        channels=int(d.get("channels", 1)),
        preprocess=MicPreprocessConfig(**_only(d.get("preprocess", {}), MicPreprocessConfig)),
        vad=VADConfig(**_only(d.get("vad", {}), VADConfig)),
    )


def _stt(d: dict[str, Any]) -> STTConfig:
    return STTConfig(
        backend=d.get("backend", "qwen_local"),
        language=d.get("language", "ja"),
        qwen_local=QwenLocalConfig(**_only(d.get("qwen_local", {}), QwenLocalConfig)),
    )


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)

    app = raw.get("app", {})
    return AppConfig(
        active_tts_profile=app.get("active_tts_profile", "gpt-sovits"),
        mic=_mic(raw.get("mic", {})),
        stt=_stt(raw.get("stt", {})),
        output=OutputConfig(**_only(raw.get("output", {}), OutputConfig)),
        api=APIConfig(**_only(raw.get("api", {}), APIConfig)),
        ui=UIConfig(**_only(raw.get("ui", {}), UIConfig)),
        config_path=path,
        profiles_dir=path.parent / "profiles",
    )
