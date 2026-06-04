"""test_local_input_server: LocalInputServer の WS 挙動テスト（実 websockets）。"""
from __future__ import annotations

import asyncio
import json

import websockets

from text_to_shaberi.client.text_input import LocalInputServer


def _port(srv):
    return srv._server.sockets[0].getsockname()[1]


def test_text_and_final_callbacks():
    async def go():
        rec_text = []
        rec_final = []
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: rec_text.append((t, p)),
            on_final=lambda: rec_final.append(1),
            on_cancel=lambda: None,
        )
        await srv.start()
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            await c.send(json.dumps({"text": "あ"}))
            r1 = json.loads(await c.recv())
            await c.send(json.dumps({"final": True}))
            r2 = json.loads(await c.recv())
            assert r1 == {"event": "accepted"}
            assert r2 == {"event": "accepted"}
        await asyncio.sleep(0.05)
        assert rec_text == [("あ", None)]
        assert len(rec_final) >= 1
        await srv.close()

    asyncio.run(go())


def test_unauthorized_closes():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            token="secret",
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        raised = False
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            try:
                await asyncio.wait_for(c.recv(), timeout=2.0)
            except websockets.ConnectionClosed:
                raised = True
        assert raised
        await srv.close()

    asyncio.run(go())


def test_busy_second_connection():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        url = f"ws://127.0.0.1:{_port(srv)}"
        async with websockets.connect(url) as c1:
            await c1.send(json.dumps({"text": "x"}))
            await c1.recv()
            async with websockets.connect(url) as c2:
                msg = json.loads(await c2.recv())
                assert msg == {"event": "error", "message": "busy"}
        await srv.close()

    asyncio.run(go())


def test_invalid_json():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            await c.send("{not json")
            msg = json.loads(await c.recv())
            assert msg["event"] == "error"
        await srv.close()

    asyncio.run(go())


def test_notify_done():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            await c.send(json.dumps({"text": "x"}))
            await c.recv()
            await srv.notify({"event": "done"})
            msg = json.loads(await c.recv())
            assert msg == {"event": "done"}
        await srv.close()

    asyncio.run(go())


def test_nonloopback_requires_token():
    async def go():
        srv = LocalInputServer(
            host="0.0.0.0",
            port=0,
            token="",
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        raised = False
        try:
            await srv.start()
        except RuntimeError:
            raised = True
        assert raised

    asyncio.run(go())
