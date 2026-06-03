"""WS セッションオーケストレータ（S2: speak/cancel/stt_begin/binary/stt_end）。

1接続につき1 Session。全送信は _send_model/_send_bytes/_send_stopped_best_effort
に一元化し asyncio.Lock で直列化する。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

import numpy as np
from fastapi import WebSocket

from ..common.opus_codec import (
    OPUS_CODEC_NAME,
    OpusStreamEncoder,
    opus_available,
    opus_wire_format,
)
from ..common.pcm import _pcm_to_float32, bytes_per_sample
from ..common.protocol import (
    AudioBegin,
    AudioEnd,
    AudioFormatPayload,
    Cancel,
    ErrorMsg,
    Hello,
    ProfileInfo,
    Ready,
    Speak,
    Stopped,
    SttBegin,
    SttEnd,
    Transcript,
    parse_message,
)
from .tts.profile import TTSProfile, load_profile

logger = logging.getLogger(__name__)

_STT_MAX_SECONDS = 60


@dataclass(slots=True)
class BinaryFrame:
    data: bytes


@dataclass(slots=True)
class SessionState:
    config: object
    profiles_dir: str
    tts_client: object
    available_profiles: list[ProfileInfo]
    stt_engine: object | None = None
    stt_executor: object | None = None
    language: str = "ja"


class Session:
    def __init__(self, ws: WebSocket, state: SessionState) -> None:
        self.ws = ws
        self.state = state
        self.session_id = uuid4().hex
        self.active_stream_id: str | None = None
        self.active_task: asyncio.Task | None = None
        self.active_parent_id: str | None = None
        self.active_stt_id: str | None = None
        self.stt_buffer: bytearray = bytearray()
        self.stt_format: AudioFormatPayload | None = None
        self.stt_auto_tts: bool = False
        self._send_lock = asyncio.Lock()
        self._handshake_done = False
        self.codec: str = "pcm_s16le"

    # ------------------------------------------------------------------
    # busy / capture helpers
    # ------------------------------------------------------------------
    def _busy(self) -> bool:
        return (
            self.active_stt_id is not None
            or (self.active_task is not None and not self.active_task.done())
        )

    def _reset_stt_capture(self) -> None:
        self.active_stt_id = None
        self.stt_buffer = bytearray()
        self.stt_format = None
        self.stt_auto_tts = False

    # ------------------------------------------------------------------
    # 送信一元化
    # ------------------------------------------------------------------
    async def _send_model(self, model: object) -> None:
        text = model.model_dump_json()
        async with self._send_lock:
            await self.ws.send_text(text)

    async def _send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(data)

    async def _send_stopped_best_effort(self, stream_id: str) -> None:
        try:
            await self._send_model(Stopped(id=stream_id))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 受信ヘルパ
    # ------------------------------------------------------------------
    async def _receive_message(self) -> object | None:
        msg = await self.ws.receive()
        if "text" in msg:
            try:
                data = json.loads(msg["text"])
            except (json.JSONDecodeError, TypeError):
                return ErrorMsg(id=None, message="invalid JSON")
            try:
                return parse_message(data)
            except ValueError:
                return ErrorMsg(id=None, message=f"unknown message type: {data.get('type', '')}")
        if "bytes" in msg:
            return BinaryFrame(data=msg["bytes"])
        return None

    # ------------------------------------------------------------------
    # メインループ
    # ------------------------------------------------------------------
    async def run(self) -> None:
        try:
            await self._handshake()
            if not self._handshake_done:
                return
            await self._message_loop()
        except Exception:
            logger.debug("[%s] セッション例外", self.session_id, exc_info=True)
        finally:
            await self._cleanup()

    # ------------------------------------------------------------------
    # ハンドシェイク
    # ------------------------------------------------------------------
    async def _handshake(self) -> None:
        from .config import ServerConfig, normalize_transport_codec

        cfg: ServerConfig = self.state.config
        parsed = await self._receive_message()
        if parsed is None:
            await self._close_after_error(None, "no message received")
            return

        if isinstance(parsed, ErrorMsg):
            await self._close_after_error(parsed.id, parsed.message)
            return

        if not isinstance(parsed, Hello):
            await self._close_after_error(None, f"first message must be hello, got {type(parsed).__name__}")
            return

        profile_name = cfg.tts.active_profile
        try:
            active_profile = load_profile(self.state.profiles_dir, profile_name)
        except Exception as exc:
            await self._close_after_error(None, f"failed to load active profile '{profile_name}': {exc}")
            return

        transport_codec = normalize_transport_codec(cfg.transport.codec)
        client_caps = parsed.caps.get("codecs", []) if isinstance(parsed.caps, dict) else []
        avail = opus_available()

        if (
            OPUS_CODEC_NAME in client_caps
            and transport_codec == OPUS_CODEC_NAME
            and avail
        ):
            chosen = OPUS_CODEC_NAME
        else:
            chosen = "pcm_s16le"

        if transport_codec == OPUS_CODEC_NAME and chosen != OPUS_CODEC_NAME:
            reasons = []
            if OPUS_CODEC_NAME not in client_caps:
                reasons.append("client caps non-opus")
            if not avail:
                reasons.append("opuslib unavailable")
            logger.warning(
                "[%s] transport.codec=opus だが pcm にフォールバック: %s",
                self.session_id,
                ", ".join(reasons),
            )

        self.codec = chosen

        fmt = AudioFormatPayload(
            codec="pcm_s16le",
            sample_rate=active_profile.audio.sample_rate,
            channels=active_profile.audio.channels,
        )
        limits: dict[str, Any] = {
            "max_chunk_bytes": 65536,
            "opus": {
                "available": avail,
                "rate": 48000,
                "frame_ms": 20,
                "bitrate": cfg.transport.opus_bitrate,
            },
        }
        ready = Ready(
            session_id=self.session_id,
            server_time=time.time(),
            codec=chosen,
            tts=fmt,
            profiles=self.state.available_profiles,
            limits=limits,
            features={"auto_tts": True},
        )
        await self._send_model(ready)
        self._handshake_done = True

    async def _close_after_error(self, msg_id: str | None, message: str) -> None:
        try:
            await self._send_model(ErrorMsg(id=msg_id, message=message))
        except Exception:
            pass
        try:
            await self.ws.close(code=1000)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # メッセージループ
    # ------------------------------------------------------------------
    async def _message_loop(self) -> None:
        while True:
            parsed = await self._receive_message()
            if parsed is None:
                continue

            if isinstance(parsed, ErrorMsg):
                await self._send_model(ErrorMsg(id=None, message=parsed.message))
                continue

            if isinstance(parsed, BinaryFrame):
                await self._handle_binary(parsed.data)
            elif isinstance(parsed, SttBegin):
                await self._handle_stt_begin(parsed)
            elif isinstance(parsed, SttEnd):
                await self._handle_stt_end(parsed)
            elif isinstance(parsed, Speak):
                await self._handle_speak(parsed)
            elif isinstance(parsed, Cancel):
                await self._handle_cancel(parsed)
            else:
                type_name = type(parsed).__name__
                await self._send_model(ErrorMsg(id=None, message=f"unsupported: {type_name}"))

    # ------------------------------------------------------------------
    # speak
    # ------------------------------------------------------------------
    async def _handle_speak(self, msg: Speak) -> None:
        if not msg.id:
            await self._send_model(ErrorMsg(id=None, message="speak.id is required"))
            return

        if self._busy():
            await self._send_model(ErrorMsg(id=msg.id, message="busy: stream already active"))
            return

        from .config import ServerConfig

        cfg: ServerConfig = self.state.config
        profile_name = msg.profile or cfg.tts.active_profile
        try:
            profile = load_profile(self.state.profiles_dir, profile_name)
        except Exception as exc:
            await self._send_model(ErrorMsg(id=msg.id, message=f"failed to load profile '{profile_name}': {exc}"))
            return

        self.active_stream_id = msg.id
        self.active_parent_id = None
        self.active_task = asyncio.create_task(
            self._stream_tts(msg.id, profile, msg.text)
        )

    async def _stream_tts(
        self,
        stream_id: str,
        profile: TTSProfile,
        text: str,
        parent_id: str | None = None,
        manage_active: bool = True,
    ) -> None:
        from .config import ServerConfig

        cfg: ServerConfig = self.state.config

        if self.codec == OPUS_CODEC_NAME:
            fmt = opus_wire_format()
            await self._send_model(AudioBegin(id=stream_id, parent_id=parent_id, format=fmt))
            try:
                enc = OpusStreamEncoder(
                    src_rate=profile.audio.sample_rate,
                    bitrate=cfg.transport.opus_bitrate,
                )
                src_channels = profile.audio.channels
                src_codec = profile.audio.encoding or "pcm_s16le"
                async for chunk in self.state.tts_client.stream(profile, text):
                    mono = self._to_mono_int16(chunk, src_codec, src_channels)
                    for pkt in enc.feed(mono):
                        await self._send_bytes(pkt)
                for pkt in enc.flush():
                    await self._send_bytes(pkt)
                await self._send_model(AudioEnd(id=stream_id))
            except asyncio.CancelledError:
                await self._send_stopped_best_effort(stream_id)
                raise
            except Exception as exc:
                await self._send_model(ErrorMsg(id=stream_id, message=repr(exc)))
            finally:
                if manage_active:
                    task = asyncio.current_task()
                    if self.active_task is task and self.active_stream_id == stream_id:
                        self.active_task = None
                        self.active_stream_id = None
                        self.active_parent_id = None
        else:
            fmt = AudioFormatPayload(
                codec="pcm_s16le",
                sample_rate=profile.audio.sample_rate,
                channels=profile.audio.channels,
            )
            await self._send_model(AudioBegin(id=stream_id, parent_id=parent_id, format=fmt))
            try:
                async for chunk in self.state.tts_client.stream(profile, text):
                    await self._send_bytes(chunk)
                await self._send_model(AudioEnd(id=stream_id))
            except asyncio.CancelledError:
                await self._send_stopped_best_effort(stream_id)
                raise
            except Exception as exc:
                await self._send_model(ErrorMsg(id=stream_id, message=repr(exc)))
            finally:
                if manage_active:
                    task = asyncio.current_task()
                    if self.active_task is task and self.active_stream_id == stream_id:
                        self.active_task = None
                        self.active_stream_id = None
                        self.active_parent_id = None

    @staticmethod
    def _to_mono_int16(chunk: bytes, codec: str, channels: int) -> bytes:
        if channels <= 1:
            return chunk
        bps = bytes_per_sample(codec)
        n_total = len(chunk) // bps
        n_frames = n_total // channels
        if n_frames == 0:
            return b""
        arr = _pcm_to_float32(chunk, codec, channels)
        mono = arr.mean(axis=1)
        return (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()

    # ------------------------------------------------------------------
    # stt_begin / binary / stt_end
    # ------------------------------------------------------------------
    async def _handle_stt_begin(self, msg: SttBegin) -> None:
        if not msg.id:
            await self._send_model(ErrorMsg(id=None, message="stt_begin.id is required"))
            return

        if self._busy():
            await self._send_model(ErrorMsg(id=msg.id, message="busy: stream already active"))
            return

        self.active_stt_id = msg.id
        self.stt_buffer = bytearray()
        self.stt_format = msg.format
        self.stt_auto_tts = msg.auto_tts

    async def _handle_binary(self, data: bytes) -> None:
        if self.active_stt_id is None:
            await self._send_model(ErrorMsg(id=None, message="no active stt stream"))
            return

        self.stt_buffer += data
        fmt = self.stt_format
        max_bytes = int(_STT_MAX_SECONDS * fmt.sample_rate * bytes_per_sample(fmt.codec) * fmt.channels)
        if len(self.stt_buffer) > max_bytes:
            await self._send_model(ErrorMsg(id=self.active_stt_id, message="stt audio too large"))
            self._reset_stt_capture()

    async def _handle_stt_end(self, msg: SttEnd) -> None:
        if msg.id != self.active_stt_id:
            await self._send_model(ErrorMsg(id=msg.id, message="no active stt stream"))
            return

        fmt = self.stt_format
        auto_tts = self.stt_auto_tts
        stt_id = self.active_stt_id

        frame_bytes = bytes_per_sample(fmt.codec) * fmt.channels
        usable = len(self.stt_buffer) - (len(self.stt_buffer) % frame_bytes)
        buf = bytes(self.stt_buffer[:usable])

        self._reset_stt_capture()
        self.active_stream_id = stt_id
        self.active_parent_id = None
        self.active_task = asyncio.create_task(
            self._process_stt(stt_id, buf, fmt, auto_tts)
        )

    # ------------------------------------------------------------------
    # _process_stt / _run_auto_tts
    # ------------------------------------------------------------------
    async def _process_stt(
        self,
        stt_id: str,
        buf: bytes,
        fmt: AudioFormatPayload,
        auto_tts: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        try:
            samples = _pcm_to_float32(buf, fmt.codec, fmt.channels)
            if samples.size == 0:
                await self._send_model(Transcript(id=stt_id, text="", final=True))
            else:
                text = await loop.run_in_executor(
                    self.state.stt_executor,
                    self.state.stt_engine.transcribe,
                    samples,
                    fmt.sample_rate,
                    self.state.language,
                )
                await self._send_model(Transcript(id=stt_id, text=text, final=True))
                if auto_tts and text.strip():
                    await self._run_auto_tts(stt_id, text)
        except asyncio.CancelledError:
            # auto_tts の TTS 中(active_parent_id 設定済)は _stream_tts が既に stopped を送出済み。
            # 二重送信を避け、STT 推論中(parent_id None)の cancel のみここで stopped を送る。
            if self.active_parent_id is None:
                await self._send_stopped_best_effort(self.active_stream_id or stt_id)
            raise
        except Exception as exc:
            await self._send_model(ErrorMsg(id=stt_id, message=repr(exc)))
        finally:
            task = asyncio.current_task()
            if self.active_task is task:
                self.active_task = None
                self.active_stream_id = None
                self.active_parent_id = None

    async def _run_auto_tts(self, stt_id: str, text: str) -> None:
        from .config import ServerConfig

        cfg: ServerConfig = self.state.config
        try:
            profile = load_profile(self.state.profiles_dir, cfg.tts.active_profile)
        except Exception as exc:
            await self._send_model(ErrorMsg(id=stt_id, message=repr(exc)))
            return

        tts_id = f"tts-{stt_id}"
        self.active_stream_id = tts_id
        self.active_parent_id = stt_id
        await self._stream_tts(tts_id, profile, text, parent_id=stt_id, manage_active=False)

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------
    async def _handle_cancel(self, msg: Cancel) -> None:
        if msg.id == self.active_stt_id:
            self._reset_stt_capture()
            await self._send_model(Stopped(id=msg.id))
        elif (
            self.active_task is not None
            and not self.active_task.done()
            and msg.id in {self.active_stream_id, self.active_parent_id}
        ):
            self.active_task.cancel()
        else:
            await self._send_model(ErrorMsg(id=msg.id, message="no active stream"))

    # ------------------------------------------------------------------
    # 切断 cleanup
    # ------------------------------------------------------------------
    async def _cleanup(self) -> None:
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
            try:
                await self.active_task
            except (asyncio.CancelledError, Exception):
                pass
        self.active_task = None
        self.active_stream_id = None
        self.active_parent_id = None
        self._reset_stt_capture()
