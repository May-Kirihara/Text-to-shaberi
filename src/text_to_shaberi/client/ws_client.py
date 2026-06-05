"""WebSocket クライアント（C1 本実装 + C2 STT/auto_tts 拡張 + X1 Opus/Security + X2a 自動再接続）。

hello→ready、speak→audio_begin+binary×N+audio_end、cancel→stopped を処理。
C2: utter(pcm)→stt_begin+binary×N+stt_end→transcript→auto_tts audio 連鎖。
X1: opus caps, Bearer token, wss/TLS ssl, opus decode, sink PCM separation.
X2a: asyncio.Lock 直列化・自動再接続(指数バックオフ+jitter)・is_connected・close/reconnect 分離.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Callable

import websockets

from ..common.opus_codec import (
    OPUS_CODEC_NAME,
    OpusStreamDecoder,
    opus_available,
    opus_wire_format,
)
from ..common.protocol import (
    AudioBegin,
    AudioEnd,
    AudioFormatPayload,
    Cancel,
    ErrorMsg,
    Hello,
    Ready,
    SegBegin,
    SegEnd,
    SegText,
    Speak,
    StreamBegin,
    StreamEnd,
    StreamStopped,
    Stopped,
    SttBegin,
    SttEnd,
    Transcript,
    parse_message,
)
from .audio.devices import resolve_device
from .audio.engine import PlaybackEngine
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
        engine_factory: Callable[..., Any] = PlaybackEngine,
        loop: asyncio.AbstractEventLoop | None = None,
        default_profile: str = "",
        stt_format: AudioFormatPayload | None = None,
        codec_pref: str = "opus",
        tls_insecure: bool = False,
        tls_cafile: str = "",
        token_in_query: bool = False,
        auto_reconnect: bool = True,
        reconnect_base: float = 0.5,
        reconnect_max_delay: float = 30.0,
        prime_ms: int = 500,
        on_audio: Callable[[str, Any], None] | None = None,
        on_monitor: Callable[[str, Any], None] | None = None,
        on_caption: Callable[[str, Any], None] | None = None,
        caption_max: int = 200,
    ) -> None:
        self._address = address
        self._token = token
        self._on_log = on_log
        self._on_state = on_state
        self._device_provider = device_provider
        self._sink_factory = sink_factory
        self._engine_factory = engine_factory
        self._loop = loop
        self._default_profile = default_profile
        self._stt_format = stt_format or AudioFormatPayload(
            codec="pcm_s16le", sample_rate=16000, channels=1
        )
        self._codec_pref = codec_pref
        self._tls_insecure = tls_insecure
        self._tls_cafile = tls_cafile
        self._token_in_query = token_in_query
        self._auto_reconnect = auto_reconnect
        self._reconnect_base = reconnect_base
        self._reconnect_max = reconnect_max_delay
        self._prime_ms = prime_ms

        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._active_id: str | None = None
        self._playback_id: str | None = None
        self._stt_auto_tts: bool = False
        self._sink: Any = None
        self._engine: Any = None
        self._ready: Ready | None = None
        self._t_send: float | None = None
        self._first_chunk_logged: bool = False
        self._cancelling: bool = False
        self._profiles: list = []
        self._send_lock = asyncio.Lock()
        self._auto_tts_warned = False
        self._playback_codec: str | None = None
        self._opus_decoder: OpusStreamDecoder | None = None

        self._conn_lock = asyncio.Lock()
        self._user_closing: bool = False
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempt: int = 0

        self._metric_kind: str | None = None
        self._last_stt_ms: float | None = None
        self._stream_mode: bool = False
        self._on_audio = on_audio
        self._capture_id: str | None = None
        self._tap_open: bool = False
        # monitor tee: 全合成音声を broadcast（capture/通常を問わず）。best-effort。
        self._on_monitor = on_monitor
        self._monitor_open: bool = False
        # caption tee: 配信字幕。AudioBegin で表示・AudioEnd(_finish_stream) で消去。
        # text-stream は音声ゲート付きで送信済みセグメントを蓄積（_caption_buf）。best-effort。
        self._on_caption = on_caption
        self._caption_open: bool = False
        self._caption_buf: list[str] = []
        self._caption_seq: int = 0
        self._caption_max: int = max(1, caption_max)
        # ローカル再生ミュート: engine/sink の出力フレームを無音化（feed は継続。
        # monitor/forward_audio は別経路で継続）。
        self._muted: bool = False

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        return asyncio.get_event_loop()

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)
        else:
            logger.info(msg)

    def _state(self, event: str, payload: Any = None) -> None:
        if self._on_state:
            self._on_state(event, payload)

    def _client_codecs(self) -> list[str]:
        if opus_available() and self._codec_pref == "opus":
            return [OPUS_CODEC_NAME, "pcm_s16le"]
        return ["pcm_s16le"]

    def _build_ssl_context(self) -> Any:
        parts = urlsplit(self._address)
        if parts.scheme != "wss":
            return None
        if self._tls_cafile:
            ctx = ssl.create_default_context(cafile=self._tls_cafile)
        elif self._tls_insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
        return ctx

    def _build_url(self) -> str:
        parts = urlsplit(self._address)
        query_params = parse_qsl(parts.query)
        if self._token and self._token_in_query:
            query_params = [(k, v) for k, v in query_params if k != "token"]
            query_params.append(("token", self._token))
        new_query = urlencode(query_params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    def is_connected(self) -> bool:
        return (
            self._ws is not None
            and not getattr(self._ws, "close_code", None)
            and self._recv_task is not None
            and not self._recv_task.done()
            and self._ready is not None
        )

    def _apply_server_settings(
        self,
        *,
        codec_pref: str | None = None,
        tls_insecure: bool | None = None,
        tls_cafile: str | None = None,
        token: str | None = None,
    ) -> None:
        if codec_pref is not None:
            self._codec_pref = codec_pref
        if tls_insecure is not None:
            self._tls_insecure = tls_insecure
        if tls_cafile is not None:
            self._tls_cafile = tls_cafile
        if token is not None:
            self._token = token

    async def _safe_close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _cancel_reconnect_task(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = None
        self._reconnect_attempt = 0

    async def connect(self) -> bool:
        async with self._conn_lock:
            if self.is_connected():
                return True
            try:
                url = self._build_url()
                connect_kwargs: dict[str, Any] = {}
                if self._token:
                    connect_kwargs["additional_headers"] = {
                        "Authorization": f"Bearer {self._token}"
                    }
                ssl_ctx = self._build_ssl_context()
                if ssl_ctx is not None:
                    connect_kwargs["ssl"] = ssl_ctx
                self._ws = await websockets.connect(url, **connect_kwargs)
                await self._send(Hello(
                    client_name="text-to-shaberi",
                    client_id="",
                    caps={"codecs": self._client_codecs()},
                ))
                raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
                msg = parse_message(json.loads(raw))
                if not isinstance(msg, Ready):
                    self._log(f"期待外のメッセージ: {msg}")
                    await self._safe_close_ws()
                    return False
                self._ready = msg
                self._profiles = msg.profiles
                self._state("ready", msg)
                self._state("connected")
                self._recv_task = self._get_loop().create_task(self._recv_loop())
                if self._engine is None:
                    self._engine = self._engine_factory(
                        loop=self._loop, on_log=self._on_log,
                    )
                self._engine.set_muted(self._muted)
                self._engine.prewarm(self._resolve_device())
                return True
            except Exception as exc:
                self._log(f"接続失敗: {exc}")
                await self._safe_close_ws()
                self._state("error", str(exc))
                return False

    async def _teardown_ws(self) -> None:
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
        if self._engine is not None:
            self._engine.abort()
            await asyncio.to_thread(self._engine.join)
        await self._safe_close_ws()
        self._ready = None

    async def close(self) -> None:
        self._user_closing = True
        self._cancel_reconnect_task()
        async with self._conn_lock:
            if self._engine is not None:
                self._engine.abort()
                await asyncio.to_thread(self._engine.close)
                self._engine = None
            await self._teardown_ws()

    async def reconnect(self, address: str | None = None, token: str | None = None) -> bool:
        self._cancel_reconnect_task()
        if address is not None:
            self._address = address
        if token is not None:
            self._token = token
        async with self._conn_lock:
            await self._teardown_ws()
        return await self.connect()

    def _schedule_reconnect(self) -> None:
        if self._user_closing or not self._auto_reconnect:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self._get_loop().create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        while not self._user_closing:
            delay = min(self._reconnect_base * (2 ** self._reconnect_attempt), self._reconnect_max)
            delay += random.uniform(0, self._reconnect_base)
            self._reconnect_attempt += 1
            self._state("reconnecting", self._reconnect_attempt)
            await asyncio.sleep(delay)
            if self._user_closing:
                return
            ok = await self.connect()
            if ok:
                self._reconnect_attempt = 0
                self._reconnect_task = None
                return
        self._reconnect_task = None

    async def speak(self, text: str, profile: str | None = None, capture: bool = False) -> bool:
        loop = self._loop or asyncio.get_event_loop()

        if not self.is_connected():
            ok = await self.connect()
            if not ok:
                self._log("接続できません")
                return False

        if self._active_id is not None:
            self._log("再生中です")
            return False

        id_ = uuid.uuid4().hex
        self._active_id = id_
        self._playback_id = None
        self._caption_buf = [text]  # speak 全文（表示は AudioBegin で）
        if capture and self._on_audio is not None:
            self._capture_id = id_
        self._stt_auto_tts = False
        self._t_send = loop.time()
        self._first_chunk_logged = False
        self._cancelling = False
        self._state("busy")
        self._metric_kind = "speak"
        self._last_stt_ms = None
        self._state("metrics", {
            "kind": "speak",
            "ttfa_ms": None,
            "stt_ms": None,
            "codec": self._ready.codec if self._ready else None,
        })

        profile_val = profile or (self._default_profile or None)

        try:
            await self._send(Speak(id=id_, text=text, profile=profile_val))
            return True
        except Exception as exc:
            self._finish_stream()
            self._state("error", str(exc))
            if not self.is_connected():
                self._schedule_reconnect()
            return False

    async def utter(self, pcm: bytes, auto_tts: bool = True, chunk_bytes: int = 32768) -> None:
        loop = self._loop or asyncio.get_event_loop()

        if not self.is_connected():
            ok = await self.connect()
            if not ok:
                self._log("接続できません")
                return

        eff_auto = auto_tts and bool(
            self._ready and self._ready.features.get("auto_tts")
        )
        if auto_tts and not eff_auto and not self._auto_tts_warned:
            self._log("[warn] サーバが auto_tts 非対応: 認識のみへ降格")
            self._auto_tts_warned = True

        if self._active_id is not None:
            self._log("発話中のためスキップ")
            return

        id_ = uuid.uuid4().hex
        self._active_id = id_
        self._playback_id = None
        self._stt_auto_tts = eff_auto
        self._cancelling = False
        self._first_chunk_logged = False
        self._t_send = loop.time()
        self._state("busy")
        self._metric_kind = "utter"
        self._last_stt_ms = None
        self._state("metrics", {
            "kind": "utter",
            "ttfa_ms": None,
            "stt_ms": None,
            "codec": self._ready.codec if self._ready else None,
        })

        fmt = AudioFormatPayload(
            codec="pcm_s16le",
            sample_rate=self._stt_format.sample_rate,
            channels=1,
        )

        try:
            async with self._send_lock:
                await self._ws_send(SttBegin(id=id_, format=fmt, auto_tts=eff_auto).model_dump_json())
                for i in range(0, len(pcm), chunk_bytes):
                    await self._ws_send(pcm[i : i + chunk_bytes])
                await self._ws_send(SttEnd(id=id_).model_dump_json())
        except Exception as exc:
            self._finish_stream()
            self._state("error", str(exc))
            if not self.is_connected():
                self._schedule_reconnect()
            return

    async def start_stream(self, auto_tts: bool = True, profile: str | None = None) -> str | None:
        if not self.is_connected():
            ok = await self.connect()
            if not ok:
                self._log("接続できません")
                return None

        if not bool(self._ready and self._ready.features.get("speak_stream")):
            return None

        if self._active_id is not None:
            return None

        loop = self._loop or asyncio.get_event_loop()
        stream_id = uuid.uuid4().hex
        self._active_id = stream_id
        self._playback_id = None
        self._stream_mode = True
        self._caption_buf = []  # 新ストリーム開始時に字幕バッファを確実にリセット
        eff_auto = auto_tts and bool(
            self._ready and self._ready.features.get("auto_tts")
        )
        self._stt_auto_tts = eff_auto
        self._cancelling = False
        self._first_chunk_logged = False
        self._t_send = loop.time()
        self._state("busy")

        fmt = AudioFormatPayload(
            codec="pcm_s16le",
            sample_rate=self._stt_format.sample_rate,
            channels=1,
        )

        try:
            await self._send(StreamBegin(id=stream_id, format=fmt, auto_tts=eff_auto, profile=profile))
        except Exception as exc:
            self._finish_stream()
            self._state("error", str(exc))
            if not self.is_connected():
                self._schedule_reconnect()
            return None
        return stream_id

    async def send_stream_segment(
        self, index: int, pcm: bytes, chunk_bytes: int = 32768
    ) -> None:
        fmt = AudioFormatPayload(
            codec="pcm_s16le",
            sample_rate=self._stt_format.sample_rate,
            channels=1,
        )
        async with self._send_lock:
            sid = self._active_id
            if sid is None or not self._stream_mode:
                return
            await self._ws_send(
                SegBegin(id=sid, index=index, format=fmt).model_dump_json()
            )
            for i in range(0, len(pcm), chunk_bytes):
                await self._ws_send(pcm[i : i + chunk_bytes])
            await self._ws_send(
                SegEnd(id=sid, index=index).model_dump_json()
            )

    async def end_stream(self) -> None:
        async with self._send_lock:
            sid = self._active_id
            if sid is None or not self._stream_mode:
                return
            await self._ws_send(StreamEnd(id=sid).model_dump_json())

    async def start_text_stream(
        self, auto_tts: bool = True, profile: str | None = None, capture: bool = False
    ) -> str | None:
        if not self.is_connected():
            ok = await self.connect()
            if not ok:
                self._log("接続できません")
                return None

        if not bool(self._ready and self._ready.features.get("text_stream")):
            return None

        if self._active_id is not None:
            return None

        loop = self._loop or asyncio.get_event_loop()
        stream_id = uuid.uuid4().hex
        self._active_id = stream_id
        self._playback_id = None
        self._stream_mode = True
        self._caption_buf = []  # 新ストリーム開始時に字幕バッファを確実にリセット
        eff_auto = auto_tts and bool(
            self._ready and self._ready.features.get("auto_tts")
        )
        self._stt_auto_tts = eff_auto
        self._cancelling = False
        self._first_chunk_logged = False
        self._t_send = loop.time()
        self._state("busy")
        if capture and self._on_audio is not None:
            self._capture_id = stream_id
        self._metric_kind = "text_stream"
        self._last_stt_ms = None
        self._state("metrics", {
            "kind": "text_stream",
            "ttfa_ms": None,
            "stt_ms": None,
            "codec": self._ready.codec if self._ready else None,
        })

        profile_val = profile or (self._default_profile or None)
        try:
            await self._send(
                StreamBegin(id=stream_id, mode="text", auto_tts=eff_auto, profile=profile_val)
            )
        except Exception as exc:
            self._finish_stream()
            self._state("error", str(exc))
            if not self.is_connected():
                self._schedule_reconnect()
            return None
        return stream_id

    async def send_text_segment(self, index: int, text: str) -> None:
        async with self._send_lock:
            sid = self._active_id
            if sid is None or not self._stream_mode:
                return
            await self._ws_send(
                SegText(id=sid, index=index, text=text).model_dump_json()
            )
            # 字幕蓄積(codex NO-GO3): _ws_send の await 中に _finish_stream が走る
            # ことがある。送信成功後、まだ同じ stream が生きている時だけ append し、
            # 終了済みなら捨てる（古いセグメントの次発話混入を防ぐ）。
            if self._active_id == sid and self._stream_mode:
                self._caption_buf.append(text)
                joined = "".join(self._caption_buf)
                if len(joined) > self._caption_max:
                    joined = joined[-self._caption_max:]
                    self._caption_buf[:] = [joined]  # 末尾優先で折りたたみ（メモリ上限化）
                if self._caption_open:
                    self._emit_caption(joined, True)

    async def end_text_stream(self) -> bool:
        async with self._send_lock:
            sid = self._active_id
            if sid is None or not self._stream_mode:
                return False
            await self._ws_send(StreamEnd(id=sid).model_dump_json())
            return True

    async def cancel(self) -> None:
        if self._active_id is None:
            self._log("cancel: active 無し")
            return
        if self._engine is not None:
            self._engine.abort()
        if self._sink is not None:
            self._sink.abort()
        self._cancelling = True
        try:
            await self._send(Cancel(id=self._active_id))
        except Exception:
            pass

    def is_busy(self) -> bool:
        return self._active_id is not None

    def set_muted(self, muted: bool) -> None:
        # ローカルスピーカー再生のミュート切替（即時）。engine/sink が出力フレームを無音化する
        # （feed/begin/end は通常どおり。monitor/forward_audio は別経路で影響なし）。
        self._muted = bool(muted)
        if self._engine is not None:
            self._engine.set_muted(self._muted)
        if self._sink is not None:
            self._sink.set_muted(self._muted)

    def _is_capture(self) -> bool:
        return self._capture_id is not None and self._active_id == self._capture_id

    def _emit_monitor(self, kind: str, payload: Any) -> None:
        # 全合成音声の broadcast tee。recv loop 上で呼ぶため、callback/broadcast の
        # 例外は再生・受信ループへ漏らさず握る（best-effort tee・codex MED3）。
        if self._on_monitor is None:
            return
        try:
            self._on_monitor(kind, payload)
        except Exception:
            self._log("monitor emit 失敗")

    def _emit_caption(self, text: str, visible: bool) -> None:
        # 配信字幕の tee。recv loop / send タスク上で呼ぶため例外は握る（best-effort）。
        if self._on_caption is None:
            return
        self._caption_seq += 1
        try:
            self._on_caption(
                "caption",
                {"text": text, "visible": visible, "seq": self._caption_seq},
            )
        except Exception:
            self._log("caption emit 失敗")

    def _maybe_log_ttfa(self) -> None:
        if self._first_chunk_logged:
            return
        self._first_chunk_logged = True
        loop = self._loop or asyncio.get_event_loop()
        if self._t_send is not None:
            ttfa_ms = (loop.time() - self._t_send) * 1000
            self._log(f"TTFA: {ttfa_ms:.0f} ms")
            self._state("metrics", {
                "kind": self._metric_kind or "speak",
                "ttfa_ms": ttfa_ms,
                "stt_ms": self._last_stt_ms,
                "codec": self._ready.codec if self._ready else None,
            })

    async def _send(self, model: Any) -> None:
        async with self._send_lock:
            await self._ws.send(model.model_dump_json())

    async def _ws_send(self, payload: Any) -> None:
        await self._ws.send(payload)

    def _resolve_device(self) -> int | None:
        if self._device_provider is None:
            return None
        raw = self._device_provider()
        if raw == "__ttsvirt__":
            self._log("ルーティングは後続（TTS_Virtual → device=None）")
            return None
        return resolve_device(raw)

    async def _recv_loop(self) -> None:
        # cancelled=True は「自分(teardown/close/reconnect)が cancel した」ことを示す。
        # サーバ正常切断(async for 正常終了)/ConnectionClosed/例外 は cancelled=False＝
        # 予期せぬ切断なので自動再接続する。手動 reconnect の幻 reconnecting を防ぐ。
        cancelled = False
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
        except asyncio.CancelledError:
            cancelled = True
        except websockets.ConnectionClosed:
            self._log("接続切断")
        except Exception as exc:
            self._log(f"受信エラー: {exc}")
        finally:
            if self._recv_task is asyncio.current_task():
                self._recv_task = None
            if self._active_id is not None:
                if self._engine is not None:
                    self._engine.abort()
                if self._sink is not None:
                    self._sink.abort()
                self._finish_stream()
            self._state("disconnected")
            if not cancelled and not self._user_closing and self._auto_reconnect:
                self._get_loop().call_soon(self._schedule_reconnect)

    def _on_binary(self, raw: bytes) -> None:
        if not self._active_id or not self._playback_id or self._cancelling:
            return

        if self._playback_codec == OPUS_CODEC_NAME:
            try:
                pcm = self._opus_decoder.decode(raw)
            except Exception:
                self._log("opus decode 失敗")
                if self._engine is not None:
                    self._engine.abort()
                self._cancelling = True
                self._finish_stream()
                self._state("error", "opus decode failed")
                return
        else:
            pcm = raw

        # monitor tee: 全ストリームの復号 pcm を broadcast（capture/通常を問わず）。
        self._emit_monitor("chunk", pcm)

        # capture stream: ローカル再生せず、復号済み pcm を tap へ流す（§10.5）
        if self._is_capture():
            if self._on_audio is not None:
                self._on_audio("chunk", pcm)
            self._maybe_log_ttfa()
            return

        sink = self._engine if self._engine is not None else self._sink
        ok = sink.feed(pcm)
        if not ok:
            if self._engine is not None:
                self._engine.abort()
            if self._sink is not None:
                self._sink.abort()
            self._cancelling = True
            self._log("再生が追いつきません")

            async def _cancel_after_overload() -> None:
                try:
                    await self._send(Cancel(id=self._active_id))
                except Exception:
                    pass

            self._get_loop().create_task(_cancel_after_overload())
            return

        self._maybe_log_ttfa()

    def _on_message(self, msg: Any) -> None:
        if isinstance(msg, Ready):
            self._ready = msg
            self._profiles = msg.profiles
            return

        if isinstance(msg, AudioBegin):
            if (
                not self._cancelling
                and self._active_id is not None
                and self._playback_id is None  # 二重 begin は side-band 扱い（monitor framing 保護）
                and (msg.id == self._active_id or msg.parent_id == self._active_id)
            ):
                self._playback_id = msg.id
                fmt = msg.format
                if fmt is None and self._ready is not None:
                    fmt = self._ready.tts
                if fmt is None or fmt.codec not in (OPUS_CODEC_NAME, "pcm_s16le"):
                    self._log(f"AudioBegin format 不正: {fmt}")
                    if self._engine is not None:
                        self._engine.abort()
                    self._cancelling = True
                    self._finish_stream()
                    self._state("error", "invalid audio format")
                    return

                is_opus = fmt.codec == OPUS_CODEC_NAME
                if is_opus:
                    expected = opus_wire_format()
                    if (
                        fmt.codec != expected.codec
                        or fmt.sample_rate != expected.sample_rate
                        or fmt.channels != expected.channels
                    ):
                        self._log("AudioBegin format が opus_wire_format と不一致")
                    self._playback_codec = OPUS_CODEC_NAME
                    self._opus_decoder = OpusStreamDecoder()
                    decoded_sr, decoded_ch = 48000, 1
                    sink_fmt = AudioFormatPayload(
                        codec="pcm_s16le", sample_rate=48000, channels=1
                    )
                else:
                    self._playback_codec = "pcm_s16le"
                    self._opus_decoder = None
                    decoded_sr, decoded_ch = fmt.sample_rate, fmt.channels
                    sink_fmt = fmt

                # monitor tee: 全ストリームの復号後フォーマットを broadcast（capture/通常を問わず）。
                self._monitor_open = True
                self._emit_monitor("begin", {
                    "codec": "pcm_s16le",
                    "sample_rate": decoded_sr,
                    "channels": decoded_ch,
                })

                # caption tee: 表示開始（現在の蓄積テキストを出す。capture でも出す＝
                # 受信側が喋るので「今喋ってる内容」。monitor と同じく capture 非依存）。
                self._caption_open = True
                self._emit_caption("".join(self._caption_buf), True)

                if self._is_capture():
                    # capture: ローカル再生はせず、復号後フォーマットを tap begin として通知（§10.2）
                    self._tap_open = True
                    if self._on_audio is not None:
                        self._on_audio("begin", {
                            "codec": "pcm_s16le",
                            "sample_rate": decoded_sr,
                            "channels": decoded_ch,
                        })
                else:
                    device = self._resolve_device()
                    if self._engine is not None:
                        self._engine.begin(sink_fmt, device)
                    else:
                        self._sink = self._sink_factory(
                            loop=self._loop,
                            on_log=self._on_log,
                            prime_ms=self._prime_ms,
                        )
                        self._sink.set_muted(self._muted)
                        self._sink.begin(sink_fmt, device)
            else:
                self._log(f"side-band audio_begin (id={msg.id})")
            return

        if isinstance(msg, Transcript):
            if msg.id == self._active_id:
                if self._stream_mode:
                    # 字幕は app 側 _on_state("transcript") が1回ログする（二重ログ回避・codex §8.7）
                    # mic streaming の字幕は最新の認識文に差し替え（音声 open 中のみ反映）。
                    self._caption_buf = [msg.text]
                    if self._caption_open:
                        self._emit_caption(msg.text, True)
                    self._state("transcript", msg.text)
                    return
                text = msg.text
                self._caption_buf = [text]  # 認識文（表示は後続 AudioBegin で）
                self._log(f"🎤→ {text}")
                self._state("transcript", text)
                if self._t_send is not None and self._metric_kind == "utter":
                    loop = self._loop or asyncio.get_event_loop()
                    stt_ms = (loop.time() - self._t_send) * 1000
                    self._last_stt_ms = stt_ms
                    self._state("metrics", {
                        "kind": "utter",
                        "ttfa_ms": None,
                        "stt_ms": stt_ms,
                        "codec": self._ready.codec if self._ready else None,
                    })
                if self._cancelling:
                    self._finish_stream()
                elif not (self._stt_auto_tts and text.strip()):
                    self._finish_stream()
            return

        if isinstance(msg, AudioEnd):
            if msg.id == self._playback_id:
                if self._cancelling or self._is_capture():
                    # capture は engine 未 begin。tap end は _finish_stream が出す（§10.2）。
                    self._finish_stream()
                else:
                    if self._engine is not None:
                        self._engine.end()
                    if self._sink is not None:
                        self._sink.end()
                    self._finish_stream()
            elif msg.id == self._active_id and self._playback_id is None:
                # 音声ゼロで正常終了した stream（空テキスト等）: 再生は無いが
                # active を解放して idle 復帰させる(§14.1)。
                self._finish_stream()
            return

        if isinstance(msg, Stopped):
            if msg.id in {self._active_id, self._playback_id}:
                self._log("stopped")
                self._finish_stream()
            return

        if isinstance(msg, StreamStopped):
            if msg.id == self._active_id:
                self._log("stream stopped")
                self._finish_stream()
            return

        if isinstance(msg, ErrorMsg):
            msg_id = getattr(msg, "id", None)
            if msg_id in {self._active_id, self._playback_id}:
                if msg_id == self._playback_id:
                    self._log(f"TTS失敗: {msg.message}")
                else:
                    self._log(f"STT/プロファイル失敗: {msg.message}")
                if self._cancelling:
                    self._finish_stream()
                else:
                    if self._engine is not None:
                        self._engine.abort()
                    if self._sink is not None:
                        self._sink.abort()
                    self._finish_stream()
                    self._state("error", msg.message)
            else:
                self._log(f"side-band error: {msg.message}")
            return

        self._log(f"無視: {getattr(msg, 'type', '?')}")

    def _finish_stream(self) -> None:
        # monitor tee の音声終端を全終了経路で1回だけ broadcast（begin 未発火なら出さない）。
        if self._monitor_open:
            self._monitor_open = False
            self._emit_monitor("end", None)
        # caption tee: 音声終端で字幕を消去（begin 未発火＝未表示なら出さない・1回だけ）。
        if self._caption_open:
            self._caption_open = False
            self._emit_caption("", False)
        self._caption_buf = []
        # capture stream の音声終端を全終了経路で1回だけ通知（§10.2）。
        if self._tap_open:
            self._tap_open = False
            if self._on_audio is not None:
                self._on_audio("end", None)
        self._capture_id = None
        self._sink = None
        self._active_id = None
        self._playback_id = None
        self._stt_auto_tts = False
        self._cancelling = False
        self._first_chunk_logged = False
        self._playback_codec = None
        self._opus_decoder = None
        self._stream_mode = False
        self._state("idle")
