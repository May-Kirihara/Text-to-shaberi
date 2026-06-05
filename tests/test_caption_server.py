"""CaptionServer（配信字幕オーバーレイ HTTP+WS）のテスト。

FakeConn を _handle に流して latest-wins coalesce・mid-join（visible問わず）・
process_request ルーティング・Origin/token 認証・close を検証する。
"""
import asyncio
import json
from types import SimpleNamespace

from websockets.http11 import Response

from text_to_shaberi.client.caption_server import CaptionServer

_STOP = object()


class FakeConn:
    def __init__(self, *, block_send: bool = False) -> None:
        self.sent: list = []
        self.closed = False
        self.request = SimpleNamespace(path="/ws", headers={})
        self._q: asyncio.Queue = asyncio.Queue()
        self._gate = asyncio.Event()
        if not block_send:
            self._gate.set()

    async def send(self, data) -> None:
        await self._gate.wait()
        self.sent.append(data)

    async def close(self, code=None, reason=None) -> None:
        self.closed = True
        self._q.put_nowait(_STOP)

    def unblock(self) -> None:
        self._gate.set()

    def disconnect(self) -> None:
        self._q.put_nowait(_STOP)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is _STOP:
            raise StopAsyncIteration
        return item


async def _settle() -> None:
    for _ in range(6):
        await asyncio.sleep(0.005)


def _msgs(conn: FakeConn) -> list[dict]:
    return [json.loads(m) for m in conn.sent]


def _req(path: str, headers: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(path=path, headers=headers or {})


# ── 1. broadcast show → clear ──────────────────────────────────────


def test_broadcast_show_then_clear():
    async def go():
        srv = CaptionServer()
        c1 = FakeConn()
        t1 = asyncio.ensure_future(srv._handle(c1))
        await _settle()

        srv.broadcast("caption", {"text": "あ", "visible": True, "seq": 1})
        await _settle()
        srv.broadcast("caption", {"text": "", "visible": False, "seq": 2})
        await _settle()

        msgs = _msgs(c1)
        assert msgs[0] == {"event": "caption", "text": "あ", "visible": True, "seq": 1}
        assert msgs[1] == {"event": "caption", "text": "", "visible": False, "seq": 2}

        c1.disconnect()
        await t1
        await srv.close()

    asyncio.run(go())


# ── 2. latest-wins coalesce ────────────────────────────────────────


def test_latest_wins_coalesce():
    async def go():
        srv = CaptionServer()
        c = FakeConn(block_send=True)
        t = asyncio.ensure_future(srv._handle(c))
        await _settle()

        srv.broadcast("caption", {"text": "a", "visible": True, "seq": 1})
        srv.broadcast("caption", {"text": "ab", "visible": True, "seq": 2})
        srv.broadcast("caption", {"text": "", "visible": False, "seq": 3})
        await _settle()

        c.unblock()
        await _settle()

        msgs = _msgs(c)
        seqs = [m["seq"] for m in msgs]
        assert 2 not in seqs
        assert msgs[-1]["seq"] == 3

        c.disconnect()
        await t
        await srv.close()

    asyncio.run(go())


# ── 3. mid-join gets current show ──────────────────────────────────


def test_midjoin_gets_current_show():
    async def go():
        srv = CaptionServer()
        c1 = FakeConn()
        t1 = asyncio.ensure_future(srv._handle(c1))
        await _settle()

        srv.broadcast("caption", {"text": "hello", "visible": True, "seq": 1})
        await _settle()

        c2 = FakeConn()
        t2 = asyncio.ensure_future(srv._handle(c2))
        await _settle()

        msgs2 = _msgs(c2)
        assert msgs2[0] == {"event": "caption", "text": "hello", "visible": True, "seq": 1}

        c1.disconnect()
        c2.disconnect()
        await asyncio.gather(t1, t2)
        await srv.close()

    asyncio.run(go())


# ── 4. mid-join after clear gets clear (NO-GO1) ───────────────────


def test_midjoin_after_clear_gets_clear():
    async def go():
        srv = CaptionServer()
        c1 = FakeConn()
        t1 = asyncio.ensure_future(srv._handle(c1))
        await _settle()

        srv.broadcast("caption", {"text": "old", "visible": True, "seq": 1})
        await _settle()
        srv.broadcast("caption", {"text": "", "visible": False, "seq": 2})
        await _settle()

        c2 = FakeConn()
        t2 = asyncio.ensure_future(srv._handle(c2))
        await _settle()

        msgs2 = _msgs(c2)
        assert len(msgs2) >= 1
        assert msgs2[0]["visible"] is False

        c1.disconnect()
        c2.disconnect()
        await asyncio.gather(t1, t2)
        await srv.close()

    asyncio.run(go())


# ── 5. broadcast zero subscribers updates _cur ─────────────────────


def test_broadcast_zero_subscribers_updates_cur():
    async def go():
        srv = CaptionServer()
        srv.broadcast("caption", {"text": "x", "visible": True, "seq": 1})
        assert srv._cur == {"event": "caption", "text": "x", "visible": True, "seq": 1}

        srv.broadcast("caption", {"text": "", "visible": False, "seq": 2})
        assert srv._cur["seq"] == 2

        await srv.close()

    asyncio.run(go())


# ── 6. close cancels senders ───────────────────────────────────────


def test_close_cancels_senders():
    async def go():
        srv = CaptionServer()
        c1, c2 = FakeConn(), FakeConn()
        t1 = asyncio.ensure_future(srv._handle(c1))
        t2 = asyncio.ensure_future(srv._handle(c2))
        await _settle()
        assert len(srv._subs) == 2

        await srv.close()
        assert len(srv._subs) == 0
        assert srv._server is None

        srv.broadcast("caption", {"text": "after", "visible": True, "seq": 1})
        assert len(c1.sent) == 0
        assert len(c2.sent) == 0

        c1.disconnect()
        c2.disconnect()
        await asyncio.gather(t1, t2, return_exceptions=True)
        await _settle()

    asyncio.run(go())


# ── 6b. close flushes pending final clear (codex 実装レビュー#1) ────


def test_close_flushes_pending_clear():
    async def go():
        srv = CaptionServer()
        c = FakeConn()
        t = asyncio.ensure_future(srv._handle(c))
        await _settle()

        # clear を broadcast した直後に（settle せず＝sender 未送信のまま）close。
        # close は cancel 前に保留を流し切るので、最後の clear が確実に届く。
        srv.broadcast("caption", {"text": "", "visible": False, "seq": 9})
        await srv.close()

        msgs = _msgs(c)
        assert msgs[-1] == {"event": "caption", "text": "", "visible": False, "seq": 9}

        c.disconnect()
        await asyncio.gather(t, return_exceptions=True)

    asyncio.run(go())


# ── 6c. close は in-flight send の完了を待つ（graceful drain・round3） ──


class _SlowSendConn(FakeConn):
    """send が完了前に yield する。sender が pending=None にした後・送信完了前の
    タイミングで close が走る状況を作る（pending 監視型 drain なら clear を取りこぼす）。"""

    async def send(self, data) -> None:
        await asyncio.sleep(0.03)
        self.sent.append(data)


def test_close_waits_for_inflight_send():
    async def go():
        srv = CaptionServer()
        c = _SlowSendConn()
        t = asyncio.ensure_future(srv._handle(c))
        await _settle()

        srv.broadcast("caption", {"text": "", "visible": False, "seq": 7})
        # sender が pending を取り出して send 中（pending=None・未送信）になる。
        # graceful drain は sender の self-return（send 完了後）を await するので届く。
        await srv.close()

        assert _msgs(c)[-1] == {"event": "caption", "text": "", "visible": False, "seq": 7}

        c.disconnect()
        await asyncio.gather(t, return_exceptions=True)

    asyncio.run(go())


# ── 7. process_request serves page ─────────────────────────────────


def test_process_request_serves_page():
    async def go():
        srv = CaptionServer()
        resp = srv._process_request(None, _req("/"))
        assert isinstance(resp, Response)
        assert resp.status_code == 200
        assert resp.body.startswith(b"<")
        assert len(resp.body) > 0
        ct = resp.headers.get("Content-Type", "")
        assert "text/html" in ct

        resp404 = srv._process_request(None, _req("/unknown"))
        assert resp404.status_code == 404

        for p in ("/index.html", "/caption", "/caption.html"):
            r = srv._process_request(None, _req(p))
            assert r.status_code == 200

        await srv.close()

    asyncio.run(go())


# ── 8. process_request WS upgrade passthrough ──────────────────────


def test_process_request_ws_upgrade_passthrough():
    async def go():
        srv = CaptionServer()
        r = srv._process_request(None, _req("/ws", {"Upgrade": "websocket"}))
        assert r is None

        r2 = srv._process_request(None, _req("/other", {"Upgrade": "websocket"}))
        assert isinstance(r2, Response)
        assert r2.status_code == 404

        await srv.close()

    asyncio.run(go())


# ── 9. upgrade multivalue / case ───────────────────────────────────


def test_process_request_upgrade_multivalue():
    async def go():
        srv = CaptionServer()

        r = srv._process_request(
            None, _req("/ws", {"Upgrade": "keep-alive, websocket"})
        )
        assert r is None

        r2 = srv._process_request(
            None, _req("/ws", {"Upgrade": "WebSocket"})
        )
        assert r2 is None

        r3 = srv._process_request(
            None, _req("/ws", {"Upgrade": "WEBSOCKET"})
        )
        assert r3 is None

        await srv.close()

    asyncio.run(go())


# ── 10. authorized token ───────────────────────────────────────────


def test_authorized_token():
    async def go():
        srv = CaptionServer(token="secret")

        r = srv._process_request(None, _req("/ws?token=secret", {"Upgrade": "websocket"}))
        assert r is None

        r2 = srv._process_request(
            None, _req("/ws?token=wrong", {"Upgrade": "websocket"})
        )
        assert isinstance(r2, Response)
        assert r2.status_code == 401

        r3 = srv._process_request(
            None, _req("/ws", {"Upgrade": "websocket", "Authorization": "Bearer secret"})
        )
        assert r3 is None

        r4 = srv._process_request(
            None, _req("/ws", {"Upgrade": "websocket", "Authorization": "Bearer wrong"})
        )
        assert isinstance(r4, Response)
        assert r4.status_code == 401

        await srv.close()

    asyncio.run(go())


# ── 11. origin blocked ─────────────────────────────────────────────


def test_origin_blocked():
    async def go():
        srv = CaptionServer()

        r = srv._process_request(
            None, _req("/ws", {"Upgrade": "websocket", "Origin": "http://evil.example"})
        )
        assert isinstance(r, Response)
        assert r.status_code == 403

        r2 = srv._process_request(
            None, _req("/ws", {"Upgrade": "websocket"})
        )
        assert r2 is None

        r3 = srv._process_request(
            None,
            _req("/ws", {"Upgrade": "websocket", "Origin": "http://127.0.0.1:8772"}),
        )
        assert r3 is None

        await srv.close()

    asyncio.run(go())


# ── 12. 非ASCII トークンでも例外化しない（codex 実装レビュー#2） ───────


def test_authorized_nonascii_token_no_crash():
    async def go():
        srv = CaptionServer(token="ひみつ")
        # 正しい token は通る（bytes 比較）
        r = srv._process_request(None, _req("/ws?token=ひみつ", {"Upgrade": "websocket"}))
        assert r is None
        # 誤り token は例外化せず 401（hmac.compare_digest(str) の TypeError を回避）
        r2 = srv._process_request(None, _req("/ws?token=ちがう", {"Upgrade": "websocket"}))
        assert isinstance(r2, Response)
        assert r2.status_code == 401
        await srv.close()

    asyncio.run(go())


# ── 13. 不正な Origin 値でも例外化しない ───────────────────────────


def test_origin_malformed_denied_no_crash():
    async def go():
        srv = CaptionServer()
        # 未終端 IPv6 は urlsplit(...).hostname が ValueError を投げる → 隔離して 403
        r = srv._process_request(
            None, _req("/ws", {"Upgrade": "websocket", "Origin": "http://[::1"})
        )
        assert isinstance(r, Response)
        assert r.status_code == 403
        await srv.close()

    asyncio.run(go())
