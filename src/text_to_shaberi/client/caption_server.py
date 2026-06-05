"""caption_server: 配信字幕オーバーレイ用のローカル HTTP + WebSocket サーバ。

OBS の Browser Source が http://127.0.0.1:8772/ を開くと字幕 HTML を返し、
そのページ内 JS が ws://host:8772/ws に繋いで字幕イベントを受ける。
購読者は latest-wins mailbox（最新1件のみ保持）で、遅い購読者は自動的に
最新状態だけを受け取る。メモリリーク不可能。
"""
from __future__ import annotations

import asyncio
import hmac
import json
from importlib.resources import files as _res_files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response


_REASON = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found"}

_INLINE_HTML = (
    b"<!doctype html><html><head><meta charset=utf-8>"
    b"<style>:root{--size:48px;--color:#fff}html,body{margin:0;height:100%;"
    b"background:transparent}#c{font-size:var(--size);color:var(--color);"
    b"opacity:0;transition:opacity .2s;padding:.3em}#c.s{opacity:1}</style></head>"
    b"<body><pre id=c></pre><script>"
    b"const c=document.getElementById('c');let s=-1;"
    b"function a(m){if(typeof m.seq==='number'&&m.seq<s)return;s=m.seq??s;"
    b"if(m.visible){c.textContent=m.text||'';c.classList.add('s')}"
    b"else c.classList.remove('s')}"
    b"function go(){const u=(location.protocol==='https:'?'wss://':'ws://')"
    b"+location.host+'/ws'+location.search;const w=new WebSocket(u);"
    b"w.onmessage=e=>{try{a(JSON.parse(e.data))}catch(_){}};"
    b"w.onopen=()=>{s=-1};w.onclose=()=>setTimeout(go,1000)}go()"
    b"</script></body></html>"
)


class _Subscriber:
    __slots__ = ("conn", "pending", "event", "task")

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.pending: dict | None = None
        self.event = asyncio.Event()
        self.task: asyncio.Task | None = None


class CaptionServer:

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8772,
        token: str = "",
        template: str = "",
        on_log: Callable[[str], None] | None = None,
        loop=None,
        max_size: int = 4096,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._template = template
        self._on_log = on_log
        self._loop = loop
        self._max_size = max_size
        self._server = None
        self._closing = False
        self._subs: dict[Any, _Subscriber] = {}
        self._cur: dict | None = None

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    async def start(self) -> None:
        if self._host not in ("127.0.0.1", "::1", "localhost") and not self._token:
            raise RuntimeError(
                "CaptionServer: 非loopback bind には token が必須です"
            )
        self._server = await websockets.serve(
            self._handle,
            self._host,
            self._port,
            process_request=self._process_request,
            max_size=self._max_size,
        )
        self._log(
            f"[caption] 字幕サーバを起動: http://{self._host}:{self._port}/"
            f" （OBSブラウザソースに貼る）"
        )

    async def close(self) -> None:
        self._closing = True
        # graceful drain（codex 実装レビュー#1 round3）: sender は pending=None を
        # `await conn.send()` の前に立てるため「pending 監視」では送信完了を判定できない。
        # 代わりに各 sender を起こし、_closing を見て「保留を送り切ってから self-return」
        # させ、それを cancel ではなく await する。これで発話終了時の最後の clear が
        # 確実に配送されてから閉じる（app 側で ws.close() を先に呼ぶ順序と両方で成立）。
        # 停滞購読者（send がブロックし続ける）は timeout 後に cancel。
        for sub in self._subs.values():
            sub.event.set()
        tasks = [s.task for s in self._subs.values() if s.task is not None]
        if tasks:
            _, still = await asyncio.wait(tasks, timeout=1.0)
            for t in still:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subs.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def broadcast(self, event: str, payload: dict) -> None:
        snap = {"event": event, **payload}
        self._cur = snap
        if self._closing:
            return
        for sub in list(self._subs.values()):
            sub.pending = snap
            sub.event.set()

    async def _sender(self, sub: _Subscriber) -> None:
        try:
            while True:
                while sub.pending is None:
                    if self._closing:
                        return  # 保留が無く closing＝送り切った後の正常終了（graceful drain）
                    sub.event.clear()
                    await sub.event.wait()
                snap = sub.pending
                sub.pending = None
                await sub.conn.send(json.dumps(snap, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._drop_subscriber(sub)

    async def _handle(self, conn) -> None:
        if self._closing:
            await conn.close(code=4409, reason="closing")
            return
        sub = _Subscriber(conn)
        sub.task = asyncio.ensure_future(self._sender(sub))
        self._subs[conn] = sub
        if self._cur is not None:
            sub.pending = self._cur
            sub.event.set()
        self._log(f"[caption] 購読者接続 (計{len(self._subs)})")
        try:
            async for _ in conn:
                pass
        except Exception:
            pass
        finally:
            self._drop_subscriber(sub)
            self._log(f"[caption] 購読者切断 (計{len(self._subs)})")

    def _drop_subscriber(self, sub: _Subscriber) -> None:
        if self._subs.pop(sub.conn, None) is None:
            return
        t = sub.task
        if t is not None and t is not asyncio.current_task():
            t.cancel()
        asyncio.ensure_future(self._safe_close(sub.conn))

    async def _safe_close(self, conn) -> None:
        try:
            await conn.close()
        except Exception:
            pass

    def _process_request(self, connection, request):
        path = urlsplit(request.path).path
        upgrade = request.headers.get("Upgrade", "") or ""
        is_ws = any(
            v.strip().lower() == "websocket" for v in upgrade.split(",")
        )
        if is_ws:
            if path != "/ws":
                return self._resp(404, b"not found")
            if not self._origin_ok(request):
                return self._resp(403, b"forbidden")
            if not self._authorized(request):
                return self._resp(401, b"unauthorized")
            return None
        if path in ("/", "/index.html", "/caption", "/caption.html"):
            return self._resp(
                200,
                self._page_bytes(),
                content_type="text/html; charset=utf-8",
            )
        return self._resp(404, b"not found")

    def _resp(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> Response:
        return Response(
            status,
            _REASON.get(status, "Unknown"),
            Headers(
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                    ("Connection", "close"),
                ]
            ),
            body,
        )

    def _origin_ok(self, request) -> bool:
        # 不正な Origin 値（urlsplit が壊れる等）は例外化させず拒否（codex 実装レビュー#2）。
        try:
            origin = request.headers.get("Origin", "")
            if not origin:
                return True
            hostname = urlsplit(origin).hostname
        except Exception:
            return False
        return hostname in (
            self._host,
            "127.0.0.1",
            "localhost",
            "::1",
        )

    def _authorized(self, request) -> bool:
        if not self._token:
            return True
        # 非ASCII トークンでも壊れないよう bytes で定数時間比較し、解析例外は拒否側へ
        # 倒す（hmac.compare_digest(str) は非ASCIIで TypeError・codex 実装レビュー#2）。
        try:
            qs_token = dict(
                parse_qsl(urlsplit(request.path).query)
            ).get("token", "")
            auth = request.headers.get("Authorization", "")
            bearer = auth[7:] if auth.startswith("Bearer ") else ""
            tok = self._token.encode("utf-8")
            return hmac.compare_digest(tok, qs_token.encode("utf-8")) or (
                hmac.compare_digest(tok, bearer.encode("utf-8"))
            )
        except Exception:
            return False

    def _page_bytes(self) -> bytes:
        if self._template:
            try:
                return Path(self._template).read_bytes()
            except Exception:
                self._log("[caption] template 読み込み失敗、fallback")
        try:
            return (
                _res_files("text_to_shaberi.client")
                .joinpath("caption")
                .joinpath("index.html")
                .read_bytes()
            )
        except Exception:
            pass
        try:
            return (Path(__file__).parent / "caption" / "index.html").read_bytes()
        except Exception:
            pass
        return _INLINE_HTML
