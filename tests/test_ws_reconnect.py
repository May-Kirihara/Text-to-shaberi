"""test_ws_reconnect: X2a 自動再接続のテスト。

pytest-asyncio 不使用。asyncio.run で完結。
fake WS サーバを途中で閉じる/再開して再接続を検証。
"""
from __future__ import annotations

import asyncio
import json

import websockets

from text_to_shaberi.common.protocol import (
    AudioBegin,
    AudioEnd,
    AudioFormatPayload,
    Hello,
    Ready,
    Speak,
    parse_message,
)


class _FakeSink:
    def __init__(self, **kwargs):
        self.records = []

    def begin(self, fmt, device):
        self.records.append(("begin",))

    def feed(self, chunk):
        self.records.append(("feed",))
        return True

    def end(self):
        self.records.append(("end",))

    def abort(self):
        self.records.append(("abort",))

    def join(self, timeout=2.0):
        pass


class _SinkFactory:
    def __init__(self):
        self.last = None

    def __call__(self, **kwargs):
        s = _FakeSink(**kwargs)
        self.last = s
        return s


def _ready_json():
    return Ready(session_id="t").model_dump_json()


def _make_ws_client(port, sink_factory, states, logs=None, **kw):
    from text_to_shaberi.client.ws_client import WSClient
    return WSClient(
        f"ws://127.0.0.1:{port}/ws",
        on_log=logs.append if logs else lambda m: None,
        on_state=lambda e, p=None: states.append((e, p)),
        sink_factory=sink_factory,
        loop=asyncio.get_event_loop(),
        auto_reconnect=True,
        reconnect_base=0.01,
        reconnect_max_delay=0.05,
        **kw,
    )


class TestReconnect:
    def test_reconnect_after_server_close_and_reopen(self):
        async def go():
            sink_factory = _SinkFactory()
            states = []
            logs = []
            handler_call = [0]

            async def handler(websocket):
                handler_call[0] += 1
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(_ready_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(
                                AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                            )
                            await websocket.send(b"\x00\x00" * 50)
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = _make_ws_client(port, sink_factory, states, logs)

                ok = await ws.connect()
                assert ok is True
                assert ws.is_connected()

                await ws.speak("テスト1")
                await asyncio.sleep(0.2)

                server.close()
                await server.wait_closed()
                await asyncio.sleep(0.3)

                assert not ws.is_connected()

            await asyncio.sleep(0.2)

            async with websockets.serve(handler, "127.0.0.1", port) as server2:
                for _ in range(50):
                    await asyncio.sleep(0.05)
                    if ws.is_connected():
                        break

                assert ws.is_connected()

                await ws.speak("テスト2")
                await asyncio.sleep(0.2)

                assert sink_factory.last is not None
                rec = sink_factory.last.records
                assert any(r[0] == "feed" for r in rec)

                reconnecting_events = [s for s in states if s[0] == "reconnecting"]
                assert len(reconnecting_events) >= 1

                await ws.close()
                assert ws._reconnect_task is None

        asyncio.run(go())

    def test_user_close_no_reconnect(self):
        async def go():
            states = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(_ready_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = _make_ws_client(port, _SinkFactory(), states)

                ok = await ws.connect()
                assert ok

                await ws.close()

                await asyncio.sleep(0.3)

                assert ws._reconnect_task is None
                reconnecting_events = [s for s in states if s[0] == "reconnecting"]
                assert len(reconnecting_events) == 0

        asyncio.run(go())

    def test_connect_no_op_when_already_connected(self):
        async def go():
            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(_ready_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = _make_ws_client(port, _SinkFactory(), [])

                ok = await ws.connect()
                assert ok

                ws2 = ws._ws
                ok2 = await ws.connect()
                assert ok2
                assert ws._ws is ws2

                await ws.close()

        asyncio.run(go())

    def test_handshake_failure_ws_is_none(self):
        async def go():
            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        await websocket.send(json.dumps({"type": "error", "message": "nope"}))

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                states = []
                from text_to_shaberi.client.ws_client import WSClient
                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    sink_factory=_SinkFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )

                ok = await ws.connect()
                assert ok is False
                assert ws._ws is None

                await ws.close()

        asyncio.run(go())

    def test_is_connected_false_after_disconnect(self):
        async def go():
            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(_ready_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = _make_ws_client(port, _SinkFactory(), [])

                ok = await ws.connect()
                assert ok
                assert ws.is_connected()

                server.close()
                await server.wait_closed()
                await asyncio.sleep(0.3)

                assert not ws.is_connected()
                assert ws._ws is None or getattr(ws._ws, "close_code", None) is not None or ws._recv_task is None or ws._recv_task.done()

                await ws.close()

        asyncio.run(go())

    def test_manual_reconnect_resets_connection(self):
        async def go():
            states = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(_ready_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = _make_ws_client(port, _SinkFactory(), states)

                ok = await ws.connect()
                assert ok

                ok = await ws.reconnect()
                assert ok
                assert ws.is_connected()

                await ws.close()

        asyncio.run(go())
