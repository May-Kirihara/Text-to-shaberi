"""text_input: LLM等のテキストを文単位に分割する TextSegmenter（純ロジック・I/O無し）。

LocalInputServer（WSサーバ）は後続ステップで同ファイルに追加する。
イベントは audio 版 vad.py の StreamStart/StreamComplete を再利用し、
音声の SubSegment に対応する TextSegment を新設する。
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

import websockets

from .audio.vad import StreamComplete, StreamStart

_BOUNDARY = set("。．.!?！？\n")
_TRAILING = set("。．.!?！？)）」』】〕］｝》〉")


@dataclass(slots=True)
class TextSegment:
    index: int
    text: str


class TextSegmenter:
    """テキストを文/句単位に分割。1文字も捨てない（feed+flush の連結＝入力の連結）。"""

    def __init__(self, *, max_chars: int = 80, hard_max_chars: int = 160) -> None:
        self._max = max(1, max_chars)
        self._hard_max = max(self._max, hard_max_chars)
        self.reset()

    def reset(self) -> None:
        self._buf = ""
        self._index = 0
        self._active = False

    def feed(self, text: str) -> list:
        events: list = []
        if not text:
            return events
        if not self._active:
            events.append(StreamStart())
            self._active = True
            self._index = 0
        self._buf += text
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            seg = self._buf[:cut]
            self._buf = self._buf[cut:]
            events.append(TextSegment(index=self._index, text=seg))
            self._index += 1
        return events

    def flush(self) -> list:
        events: list = []
        if not self._active:
            return events
        if self._buf:
            events.append(TextSegment(index=self._index, text=self._buf))
            self._index += 1
        events.append(StreamComplete())
        self.reset()
        return events

    def _find_cut(self) -> int | None:
        buf = self._buf
        n = len(buf)
        for i, ch in enumerate(buf):
            if ch not in _BOUNDARY:
                continue
            if ch == ".":
                if i + 1 >= n:
                    # 末尾の '.' は次の文字が来るまで保留（3.14 の途中かもしれない）
                    continue
                if i > 0 and buf[i - 1].isdigit() and buf[i + 1].isdigit():
                    continue
            j = i + 1
            while j < n and buf[j] in _TRAILING:
                j += 1
            return j
        if n >= self._hard_max:
            window = buf[: self._max]
            for k in range(len(window) - 1, -1, -1):
                if window[k] in "、，,　 \t":
                    return k + 1
            return self._max
        return None


class LocalInputServer:
    """ローカル WebSocket 入力口（AITuber等の外部テキスト投入）。

    127.0.0.1 で待受。v1 は単一アクティブ接続のみ受理し、追加接続は busy で弾く。
    受信 JSON: {"text": str, "profile"?: str, "final"?: bool} / {"type": "cancel"|"end"}。
    返信 JSON: {"event": "accepted"|"done"|"error", ...}。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8770,
        token: str = "",
        on_text: Callable[[str, str | None], str | None],
        on_final: Callable[[], str | None],
        on_cancel: Callable[[], None],
        on_log: Callable[[str], None] | None = None,
        loop=None,
        max_text: int = 20000,
        max_size: int = 65536,
        out_max: int = 512,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._on_text = on_text
        self._on_final = on_final
        self._on_cancel = on_cancel
        self._on_log = on_log
        self._loop = loop
        self._max_text = max_text
        self._max_size = max_size
        self._server = None
        self._active_conn = None
        self._closing = False
        # 制御JSON も音声も単一 outbound キュー(deque)で順序化（§10.3）。
        # request 起点で _audio_conn を固定し、音声/done はその conn のみへ送出（漏れ防止）。
        # _reserved: 旧 request の音声/done 配信中は新接続を busy で拒否し宛先を奪わせない。
        self._out_q: deque = deque()
        self._out_max = out_max
        self._out_event = asyncio.Event()
        self._sender_task = None
        self._drop_warned = False
        self._reserved = False
        self._audio_conn = None

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    async def start(self) -> None:
        if self._host not in ("127.0.0.1", "::1", "localhost") and not self._token:
            raise RuntimeError(
                "LocalInputServer: 非loopback bind には token が必須です"
            )
        self._server = await websockets.serve(
            self._handle, self._host, self._port, max_size=self._max_size
        )
        if self._sender_task is None:
            self._sender_task = asyncio.ensure_future(self._sender())
        self._log(f"[input] ローカル入力口を起動: ws://{self._host}:{self._port}")

    async def close(self) -> None:
        self._closing = True
        if self._sender_task is not None:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sender_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _authorized(self, conn) -> bool:
        if not self._token:
            return True
        try:
            qs_token = dict(parse_qsl(urlsplit(conn.request.path).query)).get("token", "")
            auth = conn.request.headers.get("Authorization", "")
        except Exception:
            return False
        bearer = auth[7:] if auth.startswith("Bearer ") else ""
        return self._token in (qs_token, bearer)

    async def _safe_send(self, conn, event: dict) -> None:
        try:
            await conn.send(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass

    def mark_active(self) -> None:
        # 実際に request(stream) が開始したときに app が呼ぶ（空 text では呼ばれない）。
        # 以後の音声/done の宛先を現 active 接続に固定し、done まで新接続を busy 拒否。
        # 空 text で予約が立ち永久 busy 化する問題を防ぐ（codex 再レビュー）。
        if not self._reserved and self._active_conn is not None:
            self._reserved = True
            self._audio_conn = self._active_conn
            self._drop_warned = False

    async def notify(self, event: dict) -> None:
        # 完了/エラー等は request 所有 conn(_audio_conn) へ。done で reservation 解除。
        self._enqueue(self._audio_conn, "json", event)
        if event.get("event") == "done":
            self._reserved = False
            self._audio_conn = None

    def _warn_drop(self) -> None:
        if not self._drop_warned:
            self._drop_warned = True
            self._log("[input] 消費が追いつかず音声フレームを破棄しています")

    def _enqueue(self, conn, kind: str, payload) -> None:
        if conn is None:
            return
        if len(self._out_q) >= self._out_max:
            # 制御イベントは絶対に捨てない。まず chunk を1つ退避して空ける（§10.3 / codex）。
            removed = False
            for i, item in enumerate(self._out_q):
                if item[1] == "chunk":
                    del self._out_q[i]
                    removed = True
                    break
            if kind == "chunk" and (not removed or len(self._out_q) >= self._out_max):
                self._warn_drop()
                return  # 音声フレームのみ drop（chunk が無いほど詰まった異常時は制御を超過許可で入れる）
        self._out_q.append((conn, kind, payload))
        self._out_event.set()

    def send_audio(self, kind: str, payload) -> None:
        # audio tap（loopスレッド）から同期呼び出し。request 所有 conn へ固定送出。
        # kind: "begin"(fmt dict)/"chunk"(bytes)/"end"(None)
        self._enqueue(self._audio_conn, kind, payload)

    async def _sender(self) -> None:
        while True:
            while not self._out_q:
                self._out_event.clear()
                await self._out_event.wait()
            conn, kind, payload = self._out_q.popleft()
            # item はタグ付けされた宛先 conn へそのまま送る。閉じていれば例外→drop。
            try:
                if kind == "json":
                    await conn.send(json.dumps(payload, ensure_ascii=False))
                elif kind == "begin":
                    await conn.send(
                        json.dumps({"event": "audio_begin", "format": payload}, ensure_ascii=False)
                    )
                elif kind == "chunk":
                    await conn.send(payload)
                elif kind == "end":
                    await conn.send(json.dumps({"event": "audio_end"}))
            except Exception:
                pass

    async def _handle(self, conn) -> None:
        if not self._authorized(conn):
            await conn.close(code=4401, reason="unauthorized")
            return
        if self._closing or self._active_conn is not None or self._reserved:
            # _reserved: 直前 request の音声/done をまだ配信中（owner が切断済みでも）。
            await self._safe_send(conn, {"event": "error", "message": "busy"})
            await conn.close(code=4409, reason="busy")
            return
        self._active_conn = conn
        try:
            async for raw in conn:
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError("not an object")
                except (ValueError, TypeError):
                    self._enqueue(conn, "json", {"event": "error", "message": "invalid json"})
                    continue
                mtype = msg.get("type")
                if mtype == "cancel":
                    # bridge.abort() が pending 破棄＋stream 中断し、音声終了(idle)後に on_done を出す。
                    # その done(notify) が reservation を解除するので、ここでは解除しない（codex#4）。
                    self._on_cancel()
                    self._enqueue(conn, "json", {"event": "accepted"})
                    continue
                err: str | None = None
                if "text" in msg:
                    text = msg.get("text")
                    if not isinstance(text, str):
                        err = "text must be string"
                    else:
                        if len(text) > self._max_text:
                            text = text[: self._max_text]
                        err = self._on_text(text, msg.get("profile"))
                if err is None and (msg.get("final") or mtype == "end"):
                    err = self._on_final()
                if err:
                    self._enqueue(conn, "json", {"event": "error", "message": err})
                else:
                    self._enqueue(conn, "json", {"event": "accepted"})
        finally:
            if self._active_conn is conn:
                self._active_conn = None
                try:
                    self._on_final()
                except Exception:
                    pass
