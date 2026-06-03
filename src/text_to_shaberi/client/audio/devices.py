"""音声デバイス列挙 (sounddevice/PortAudio)。

`sounddevice` は libportaudio2 が無いと import 時点で失敗するため遅延importする。
列挙に失敗した場合は呼び出し側で握りつぶせるよう、例外をそのまま送出する。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float


def _sd():
    import sounddevice as sd

    return sd


def list_devices(kind: str | None = None) -> list[AudioDevice]:
    """kind: None=全部 / "output" / "input"。"""
    sd = _sd()
    result: list[AudioDevice] = []
    for index, d in enumerate(sd.query_devices()):
        dev = AudioDevice(
            index=index,
            name=d["name"],
            max_input_channels=int(d["max_input_channels"]),
            max_output_channels=int(d["max_output_channels"]),
            default_samplerate=float(d["default_samplerate"]),
        )
        if kind == "output" and dev.max_output_channels <= 0:
            continue
        if kind == "input" and dev.max_input_channels <= 0:
            continue
        result.append(dev)
    return result


def list_output_devices() -> list[AudioDevice]:
    return list_devices("output")


def list_input_devices() -> list[AudioDevice]:
    return list_devices("input")


_PREFERRED_DEFAULT_NAMES = ("pipewire", "pulse", "default", "sysdefault")


def preferred_default_output() -> int | None:
    """システム既定sinkへ確実に届く出力デバイスindexを推定。

    PipeWire/pulse 系を優先する。見つからなければ None（PortAudio既定にフォールバック）。
    """
    try:
        devices = list_output_devices()
    except Exception:  # noqa: BLE001 - 列挙不可ならフォールバック
        return None
    pairs = [(d.index, d.name.lower()) for d in devices]
    for target in _PREFERRED_DEFAULT_NAMES:
        for index, name in pairs:
            if name == target:
                return index
    for index, name in pairs:
        if "pipewire" in name or "pulse" in name:
            return index
    return None


def resolve_device(name_or_default: str | int | None, kind: str = "output") -> int | None:
    """設定値（'default' / index / 名前の一部）を PortAudio デバイスindexへ解決。

    'default'/None/'' は None（=PortAudio既定）を返す。
    """
    if name_or_default in (None, "", "default"):
        return None
    try:
        return int(name_or_default)
    except (TypeError, ValueError):
        pass
    needle = str(name_or_default).lower()
    for dev in list_devices(kind):
        if needle in dev.name.lower():
            return dev.index
    raise ValueError(f"音声デバイスが見つかりません: {name_or_default!r}")
