"""WS セッションオーケストレータ（S1: speak/cancel 状態機械）。

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

from fastapi import WebSocket

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
    parse_message,
)
from .tts.profile import TTSProfile, load_profile

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionState:
    config: object
    profiles_dir: str
    tts_client: object
    available_profiles: list[ProfileInfo]


class Session:
    """1 WS 接続の状態とロジック。"""

    def __init__(self, ws: WebSocket, state: SessionState) -> None:
        self.ws = ws
        self.state = state
        self.session_id = uuid4().hex
        self.active_stream_id: str | None = None
        self.active_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._handshake_done = False

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
        """ws.receive() で dict を受け取り text/bytes を分岐。

        正常時は parsed BaseModel を返す。binary は error を返す。
        JSON 不正・不明 type も error を返す（id=None）。
        """
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
            return ErrorMsg(id=None, message="binary unsupported in S1")
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
        from .config import ServerConfig

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

        fmt = AudioFormatPayload(
            codec="pcm_s16le",
            sample_rate=active_profile.audio.sample_rate,
            channels=active_profile.audio.channels,
        )
        ready = Ready(
            session_id=self.session_id,
            server_time=time.time(),
            codec="pcm_s16le",
            tts=fmt,
            profiles=self.state.available_profiles,
            limits={"max_chunk_bytes": 65536},
            features={"auto_tts": False},
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

            if isinstance(parsed, Speak):
                await self._handle_speak(parsed)
            elif isinstance(parsed, Cancel):
                await self._handle_cancel(parsed)
            else:
                type_name = type(parsed).__name__
                await self._send_model(ErrorMsg(id=None, message=f"unsupported in S1: {type_name}"))

    # ------------------------------------------------------------------
    # speak
    # ------------------------------------------------------------------
    async def _handle_speak(self, msg: Speak) -> None:
        if not msg.id:
            await self._send_model(ErrorMsg(id=None, message="speak.id is required"))
            return

        if self.active_task is not None and not self.active_task.done():
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
        self.active_task = asyncio.create_task(
            self._stream_tts(msg.id, profile, msg.text)
        )

    async def _stream_tts(self, stream_id: str, profile: TTSProfile, text: str) -> None:
        fmt = AudioFormatPayload(
            codec="pcm_s16le",
            sample_rate=profile.audio.sample_rate,
            channels=profile.audio.channels,
        )
        await self._send_model(AudioBegin(id=stream_id, format=fmt))
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
            task = asyncio.current_task()
            if self.active_task is task and self.active_stream_id == stream_id:
                self.active_task = None
                self.active_stream_id = None

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------
    async def _handle_cancel(self, msg: Cancel) -> None:
        if self.active_stream_id == msg.id and self.active_task is not None and not self.active_task.done():
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
