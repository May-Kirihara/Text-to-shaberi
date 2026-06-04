"""pydantic v2 WSメッセージスキーマ（型定義のみ）。

状態機械ロジックは持たない（S1/C1）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class AudioFormatPayload(BaseModel):
    codec: str = "pcm_s16le"
    sample_rate: int = 32000
    channels: int = 1


class ProfileInfo(BaseModel):
    id: str
    name: str


class Hello(BaseModel):
    type: Literal["hello"] = "hello"
    protocol_version: int = 1
    client_name: str = ""
    client_id: str = ""
    caps: dict[str, Any] = {}


class Ready(BaseModel):
    type: Literal["ready"] = "ready"
    session_id: str = ""
    server_time: float | None = None
    codec: str = "pcm_s16le"
    tts: AudioFormatPayload = AudioFormatPayload()
    profiles: list[ProfileInfo] = []
    limits: dict[str, Any] = {}
    features: dict[str, Any] = {}


class Speak(BaseModel):
    type: Literal["speak"] = "speak"
    id: str = ""
    text: str = ""
    profile: str | None = None


class SttBegin(BaseModel):
    type: Literal["stt_begin"] = "stt_begin"
    id: str = ""
    format: AudioFormatPayload = AudioFormatPayload()
    auto_tts: bool = True


class SttEnd(BaseModel):
    type: Literal["stt_end"] = "stt_end"
    id: str = ""


class Transcript(BaseModel):
    type: Literal["transcript"] = "transcript"
    id: str = ""
    text: str = ""
    final: bool = True
    index: int | None = None


class AudioBegin(BaseModel):
    type: Literal["audio_begin"] = "audio_begin"
    id: str = ""
    parent_id: str | None = None
    format: AudioFormatPayload = AudioFormatPayload()


class AudioEnd(BaseModel):
    type: Literal["audio_end"] = "audio_end"
    id: str = ""


class Cancel(BaseModel):
    type: Literal["cancel"] = "cancel"
    id: str = ""


class Stopped(BaseModel):
    type: Literal["stopped"] = "stopped"
    id: str = ""


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    id: str | None = None
    message: str = ""


class StreamBegin(BaseModel):
    type: Literal["stream_begin"] = "stream_begin"
    id: str = ""
    format: AudioFormatPayload = AudioFormatPayload()
    auto_tts: bool = True


class SegBegin(BaseModel):
    type: Literal["seg_begin"] = "seg_begin"
    id: str = ""
    index: int = 0
    format: AudioFormatPayload = AudioFormatPayload()
    prev_tail: str = ""


class SegEnd(BaseModel):
    type: Literal["seg_end"] = "seg_end"
    id: str = ""
    index: int = 0


class StreamEnd(BaseModel):
    type: Literal["stream_end"] = "stream_end"
    id: str = ""


class StreamStopped(BaseModel):
    type: Literal["stream_stopped"] = "stream_stopped"
    id: str = ""


_MESSAGE_TYPES: dict[str, type[BaseModel]] = {
    "hello": Hello,
    "ready": Ready,
    "speak": Speak,
    "stt_begin": SttBegin,
    "stt_end": SttEnd,
    "transcript": Transcript,
    "audio_begin": AudioBegin,
    "audio_end": AudioEnd,
    "cancel": Cancel,
    "stopped": Stopped,
    "error": ErrorMsg,
    "stream_begin": StreamBegin,
    "seg_begin": SegBegin,
    "seg_end": SegEnd,
    "stream_end": StreamEnd,
    "stream_stopped": StreamStopped,
}


def parse_message(data: dict[str, Any]) -> BaseModel:
    """type フィールドで判別して該当モデルへ変換。"""
    msg_type = data.get("type", "")
    cls = _MESSAGE_TYPES.get(msg_type)
    if cls is None:
        raise ValueError(f"unknown message type: {msg_type}")
    return cls(**{k: v for k, v in data.items() if k != "type" or k in cls.model_fields})
