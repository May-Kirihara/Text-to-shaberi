"""audio_monitor: 全合成音声を broadcast するローカル WS 出力（口パク等の外部エンジン向け）。

127.0.0.1 で待受。接続した全クライアント（購読者）へ、クライアントが合成・再生する
全音声を発話単位で配信する:
    → {"event":"audio_begin","format":{"codec":"pcm_s16le","sample_rate":N,"channels":C}}
    → (binary) pcm_s16le フレーム × N
    → {"event":"audio_end"}

テキスト投入や予約（LocalInputServer の reservation）とは無関係の純 broadcast（tee）。
購読者は受信専用（送信は無視）。複数購読可。遅い購読者は per-subscriber キューで
chunk のみ間引き、全く drain しない死/停滞購読者は切断する（framing 保証 or 接続終了）。
合成・他購読者を一切ブロックしない。
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import websockets


class _Subscriber:
    __slots__ = ("conn", "queue", "event", "task", "drop_warned")

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.queue: deque = deque()
        self.event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.drop_warned = False


class AudioMonitorServer:
    """全合成音声を broadcast する出力モニタサーバ。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8771,
        token: str = "",
        on_log: Callable[[str], None] | None = None,
        loop=None,
        queue_max: int = 256,
        max_size: int = 65536,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._on_log = on_log
        self._loop = loop
        self._queue_max = max(1, queue_max)
        self._max_size = max_size
        self._server = None
        self._closing = False
        self._subs: dict[Any, _Subscriber] = {}
        # 進行中発話の復号後フォーマット（mid-stream join 時の begin 補完用）。
        self._cur_format: dict | None = None

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    async def start(self) -> None:
        if self._host not in ("127.0.0.1", "::1", "localhost") and not self._token:
            raise RuntimeError(
                "AudioMonitorServer: 非loopback bind には token が必須です"
            )
        self._server = await websockets.serve(
            self._handle, self._host, self._port, max_size=self._max_size
        )
        self._log(f"[monitor] 音声モニタを起動: ws://{self._host}:{self._port}")

    async def close(self) -> None:
        self._closing = True
        tasks = [s.task for s in self._subs.values() if s.task is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subs.clear()
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

    async def _safe_close(self, conn) -> None:
        try:
            await conn.close()
        except Exception:
            pass

    def broadcast(self, kind: str, payload: Any) -> None:
        """audio tee（loop スレッド）から同期呼び出し。await 無し＝iteration 中に
        他コルーチンが割り込まない。kind: "begin"(fmt dict)/"chunk"(bytes)/"end"(None)。"""
        # begin/end は購読者ゼロでも format 追跡＋診断ログ（音声が tee に届いているか可視化）。
        if kind == "begin":
            self._cur_format = payload
            self._log(f"[monitor] broadcast begin (購読者 {len(self._subs)}) format={payload}")
        elif kind == "end":
            self._cur_format = None
            self._log(f"[monitor] broadcast end (購読者 {len(self._subs)})")
        if self._closing or not self._subs:
            return
        for sub in list(self._subs.values()):
            self._enqueue(sub, kind, payload)

    def _enqueue(self, sub: _Subscriber, kind: str, payload: Any) -> None:
        q = sub.queue
        if len(q) >= self._queue_max:
            # まず chunk を1つ退避して空ける（制御イベント begin/end は捨てない）。
            removed = False
            for i, item in enumerate(q):
                if item[0] == "chunk":
                    del q[i]
                    removed = True
                    break
            if not removed:
                # chunk が無く退避できない＝sender が全く drain していない死/停滞購読者。
                # begin/end を無制限に積んでメモリリークさせず、この購読者を切断する。
                self._drop_subscriber(sub)
                return
            if kind == "chunk" and not sub.drop_warned:
                sub.drop_warned = True
                self._log("[monitor] 購読者の消費が追いつかず音声フレームを破棄しています")
        q.append((kind, payload))
        sub.event.set()

    def _drop_subscriber(self, sub: _Subscriber) -> None:
        # stall / send 失敗 / 切断 の全経路で共用。idempotent。
        if self._subs.pop(sub.conn, None) is None:
            return
        t = sub.task
        if t is not None and t is not asyncio.current_task():
            t.cancel()
        asyncio.ensure_future(self._safe_close(sub.conn))

    async def _sender(self, sub: _Subscriber) -> None:
        try:
            while True:
                while not sub.queue:
                    sub.event.clear()
                    await sub.event.wait()
                kind, payload = sub.queue.popleft()
                if kind == "begin":
                    await sub.conn.send(
                        json.dumps({"event": "audio_begin", "format": payload}, ensure_ascii=False)
                    )
                elif kind == "chunk":
                    await sub.conn.send(payload)
                elif kind == "end":
                    await sub.conn.send(json.dumps({"event": "audio_end"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 送信失敗（切断等）→ 自身をクリーンアップ（_handle の async for 終了に依存しない）。
            self._drop_subscriber(sub)

    async def _handle(self, conn) -> None:
        if not self._authorized(conn):
            await conn.close(code=4401, reason="unauthorized")
            return
        if self._closing:
            await conn.close(code=4409, reason="closing")
            return
        sub = _Subscriber(conn)
        sub.task = asyncio.ensure_future(self._sender(sub))
        self._subs[conn] = sub
        # mid-stream join: 進行中発話があれば begin を補い「chunk が begin 前」を防ぐ。
        if self._cur_format is not None:
            self._enqueue(sub, "begin", self._cur_format)
        self._log(f"[monitor] 購読者接続 (計{len(self._subs)})")
        try:
            async for _ in conn:
                pass  # 受信専用: 購読者からの入力は無視
        except Exception:
            pass
        finally:
            self._drop_subscriber(sub)
            self._log(f"[monitor] 購読者切断 (計{len(self._subs)})")
