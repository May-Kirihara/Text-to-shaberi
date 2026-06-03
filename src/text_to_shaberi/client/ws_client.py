"""WebSocket クライアント（C1 本実装）。

hello→ready、speak→audio_begin+binary×N+audio_end、cancel→stopped を処理。
受信(asyncio)→再生(別スレッド) を PlaybackSink で橋渡し。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Callable

import websockets

from ..common.protocol import (
    AudioBegin,
    AudioEnd,
    Cancel,
    ErrorMsg,
    Hello,
    Ready,
    Speak,
    Stopped,
    parse_message,
)
from .audio.devices import resolve_device
from .audio.sink import PlaybackSink

logger = logging.getLogger(__name__)


class WSClient:
    def __init__(
        self,
        address: str,
        token: str = "",
        *,
        on_log: Callable[[str], None] | None = None,
        on_state: Callable[[str, Any], None] | None = None,
        device_provider: Callable[[], Any] | None = None,
        sink_factory: Callable[..., Any] = PlaybackSink,
        loop: asyncio.AbstractEventLoop | None = None,
        default_profile: str = "",
    ) -> None:
        self._address = address
        self._token = token
        self._on_log = on_log
        self._on_state = on_state
        self._device_provider = device_provider
        self._sink_factory = sink_factory
        self._loop = loop
        self._default_profile = default_profile

        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._active_id: str | None = None
        self._sink: Any = None
        self._ready: Ready | None = None
        self._t_send: float | None = None
        self._first_chunk_logged: bool = False
        self._cancelling: bool = False
        self._profiles: list = []

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)
        else:
            logger.info(msg)

    def _state(self, event: str, payload: Any = None) -> None:
        if self._on_state:
            self._on_state(event, payload)

    def _build_url(self) -> str:
        parts = urlsplit(self._address)
        query_params = parse_qsl(parts.query)
        if self._token:
            query_params = [(k, v) for k, v in query_params if k != "token"]
            query_params.append(("token", self._token))
        new_query = urlencode(query_params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    async def connect(self) -> bool:
        try:
            url = self._build_url()
            self._ws = await websockets.connect(url)
            await self._send(Hello(
                client_name="text-to-shaberi",
                client_id="",
                caps={"codecs": ["pcm_s16le"]},
            ))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            msg = parse_message(json.loads(raw))
            if not isinstance(msg, Ready):
                self._log(f"期待外のメッセージ: {msg}")
                await self._ws.close()
                self._ws = None
                return False
            self._ready = msg
            self._profiles = msg.profiles
            self._state("ready", msg)
            self._state("connected")
            self._recv_task = asyncio.ensure_future(self._recv_loop())
            return True
        except Exception as exc:
            self._log(f"接続失敗: {exc}")
            self._state("error", str(exc))
            return False

    async def reconnect(self, address: str | None = None, token: str | None = None) -> bool:
        await self.close()
        if address is not None:
            self._address = address
        if token is not None:
            self._token = token
        return await self.connect()

    async def speak(self, text: str, profile: str | None = None) -> None:
        loop = self._loop or asyncio.get_event_loop()

        if self._ws is None:
            ok = await self.connect()
            if not ok:
                self._log("接続できません")
                return

        if self._active_id is not None:
            self._log("再生中です")
            return

        id_ = uuid.uuid4().hex
        self._active_id = id_
        self._t_send = loop.time()
        self._first_chunk_logged = False
        self._cancelling = False
        self._state("busy")

        profile_val = profile or (self._default_profile or None)

        try:
            await self._send(Speak(id=id_, text=text, profile=profile_val))
        except Exception as exc:
            self._finish_stream()
            self._state("error", str(exc))

    async def cancel(self) -> None:
        if self._active_id is None:
            self._log("cancel: active 無し")
            return
        if self._sink is not None:
            self._sink.abort()
        self._cancelling = True
        try:
            await self._send(Cancel(id=self._active_id))
        except Exception:
            pass

    async def close(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._sink is not None:
            self._sink.abort()
            await asyncio.to_thread(self._sink.join)
            self._sink = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send(self, model: Any) -> None:
        await self._ws.send(model.model_dump_json())

    def _resolve_device(self) -> int | None:
        if self._device_provider is None:
            return None
        raw = self._device_provider()
        if raw == "__ttsvirt__":
            self._log("ルーティングは後続（TTS_Virtual → device=None）")
            return None
        return resolve_device(raw)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    self._on_binary(raw)
                elif isinstance(raw, str):
                    try:
                        m = parse_message(json.loads(raw))
                    except Exception as exc:
                        self._log(f"パース失敗: {exc}")
                        continue
                    self._on_message(m)
        except websockets.ConnectionClosed:
            self._log("接続切断")
            self._state("disconnected")
            if self._active_id is not None:
                if self._sink is not None:
                    self._sink.abort()
                self._finish_stream()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"受信エラー: {exc}")
            self._state("disconnected")
            if self._active_id is not None:
                if self._sink is not None:
                    self._sink.abort()
                self._finish_stream()

    def _on_binary(self, raw: bytes) -> None:
        if not self._active_id or self._sink is None or self._cancelling:
            return

        ok = self._sink.feed(raw)
        if not ok:
            self._sink.abort()
            self._cancelling = True
            self._log("再生が追いつきません")

            async def _cancel_after_overload() -> None:
                try:
                    await self._send(Cancel(id=self._active_id))
                except Exception:
                    pass

            if self._loop is not None:
                asyncio.ensure_future(_cancel_after_overload(), loop=self._loop)
            return

        if not self._first_chunk_logged:
            self._first_chunk_logged = True
            loop = self._loop or asyncio.get_event_loop()
            if self._t_send is not None:
                ttfa_ms = (loop.time() - self._t_send) * 1000
                self._log(f"TTFA: {ttfa_ms:.0f} ms")

    def _on_message(self, msg: Any) -> None:
        if isinstance(msg, Ready):
            self._ready = msg
            self._profiles = msg.profiles
            return

        if isinstance(msg, AudioBegin):
            if msg.id == self._active_id and not self._cancelling:
                fmt = msg.format
                if fmt is None and self._ready is not None:
                    fmt = self._ready.tts
                device = self._resolve_device()
                self._sink = self._sink_factory(
                    loop=self._loop,
                    on_log=self._on_log,
                )
                self._sink.begin(fmt, device)
            else:
                self._log(f"side-band audio_begin (id={msg.id})")
            return

        if isinstance(msg, AudioEnd):
            if msg.id == self._active_id:
                if self._cancelling:
                    self._finish_stream()
                else:
                    if self._sink is not None:
                        self._sink.end()
                    self._finish_stream()
            return

        if isinstance(msg, Stopped):
            if msg.id == self._active_id:
                self._log("stopped")
                self._finish_stream()
            return

        if isinstance(msg, ErrorMsg):
            msg_id = getattr(msg, "id", None)
            if msg_id == self._active_id:
                if self._cancelling:
                    self._finish_stream()
                else:
                    if self._sink is not None:
                        self._sink.abort()
                    self._finish_stream()
                    self._state("error", msg.message)
            else:
                self._log(f"side-band error: {msg.message}")
            return

        self._log(f"無視: {getattr(msg, 'type', '?')}")

    def _finish_stream(self) -> None:
        self._sink = None
        self._active_id = None
        self._cancelling = False
        self._first_chunk_logged = False
        self._state("idle")
