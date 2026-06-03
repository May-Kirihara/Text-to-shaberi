"""test_ws_client: WSClient の fake WS サーバによるテスト。

pytest-asyncio 不使用。asyncio.run で完結。
websockets.serve で fake サーバを立てる。
"""
from __future__ import annotations

import asyncio
import json
import struct
import time

import pytest
import websockets

from text_to_shaberi.client.ws_client import WSClient
from text_to_shaberi.common.protocol import (
    AudioBegin,
    AudioEnd,
    AudioFormatPayload,
    Cancel,
    ErrorMsg,
    Hello,
    Ready,
    Speak,
    Stopped,
    parse_message,
)


class FakeSink:
    def __init__(self, **kwargs):
        self.records: list[tuple] = []
        self._loop = kwargs.get("loop")
        self._on_log = kwargs.get("on_log")

    def begin(self, fmt, device):
        self.records.append(("begin", fmt.sample_rate, device))

    def feed(self, chunk):
        self.records.append(("feed", len(chunk)))
        return True

    def end(self):
        self.records.append(("end",))

    def abort(self):
        self.records.append(("abort",))

    def join(self, timeout=2.0):
        pass


class FakeSinkFactory:
    def __init__(self):
        self.last_sink: FakeSink | None = None

    def __call__(self, **kwargs):
        sink = FakeSink(**kwargs)
        self.last_sink = sink
        return sink


async def _fake_server_handler(websocket, messages_to_send, received_messages):
    async for raw in websocket:
        if isinstance(raw, str):
            msg = parse_message(json.loads(raw))
            received_messages.append(msg)
            if isinstance(msg, Hello):
                for m in messages_to_send.get("hello_response", []):
                    if isinstance(m, bytes):
                        await websocket.send(m)
                    else:
                        await websocket.send(m.model_dump_json())
            elif isinstance(msg, Speak):
                for m in messages_to_send.get("speak_response", []):
                    if isinstance(m, bytes):
                        await websocket.send(m)
                    else:
                        await websocket.send(m.model_dump_json())
            elif isinstance(msg, Cancel):
                for m in messages_to_send.get("cancel_response", []):
                    if isinstance(m, bytes):
                        await websocket.send(m)
                    else:
                        await websocket.send(m.model_dump_json())


def _run_test(coroutine):
    return asyncio.run(coroutine)


class TestWSClient:
    def test_connect_and_speak(self):
        async def go():
            sink_factory = FakeSinkFactory()
            received: list = []

            ready_msg = Ready(
                session_id="test",
                codec="pcm_s16le",
                tts=AudioFormatPayload(codec="pcm_s16le", sample_rate=32000, channels=1),
                profiles=[],
            )
            speak_id_holder = [""]

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        received.append(msg)
                        if isinstance(msg, Hello):
                            await websocket.send(ready_msg.model_dump_json())
                        elif isinstance(msg, Speak):
                            speak_id_holder[0] = msg.id
                            await websocket.send(
                                AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                            )
                            await websocket.send(b"\x00\x00" * 100)
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                logs: list[str] = []
                states: list[tuple] = []

                def on_log(msg):
                    logs.append(msg)

                def on_state(event, payload=None):
                    states.append((event, payload))

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=on_log,
                    on_state=on_state,
                    sink_factory=sink_factory,
                    loop=asyncio.get_event_loop(),
                )
                ok = await ws.connect()
                assert ok is True

                await ws.speak("テスト")
                await asyncio.sleep(0.2)

                assert sink_factory.last_sink is not None
                rec = sink_factory.last_sink.records
                assert any(r[0] == "begin" for r in rec)
                assert any(r[0] == "feed" for r in rec)
                assert any(r[0] == "end" for r in rec)

                await ws.close()

        _run_test(go())

    def test_sideband_error_continues(self):
        async def go():
            sink_factory = FakeSinkFactory()

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(
                                AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                            )
                            await websocket.send(b"\x00\x00" * 50)
                            await websocket.send(ErrorMsg(id="other-id", message="side-band error").model_dump_json())
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    sink_factory=sink_factory,
                    loop=asyncio.get_event_loop(),
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.3)

                rec = sink_factory.last_sink.records
                assert not any(r[0] == "abort" for r in rec)

                await ws.close()

        _run_test(go())

    def test_terminal_error_aborts(self):
        async def go():
            sink_factory = FakeSinkFactory()

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(ErrorMsg(id=msg.id, message="TTS failed").model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    sink_factory=sink_factory,
                    loop=asyncio.get_event_loop(),
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.2)

                if sink_factory.last_sink is not None:
                    rec = sink_factory.last_sink.records
                    assert any(r[0] == "abort" for r in rec)

                await ws.close()

        _run_test(go())

    def test_cancel_discards_binary_and_no_end(self):
        async def go():
            sink_factory = FakeSinkFactory()
            cancel_event = asyncio.Event()
            speak_id = [""]

            async def handler(websocket):
                tasks = []
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())
                        elif isinstance(msg, Speak):
                            speak_id[0] = msg.id
                            await websocket.send(
                                AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                            )
                            await websocket.send(b"\x00\x00" * 50)

                            async def _send_after_cancel():
                                await cancel_event.wait()
                                try:
                                    await websocket.send(b"\x00\x00" * 50)
                                    await websocket.send(AudioEnd(id=speak_id[0]).model_dump_json())
                                except Exception:
                                    pass

                            tasks.append(asyncio.ensure_future(_send_after_cancel()))
                        elif isinstance(msg, Cancel):
                            cancel_event.set()
                            await websocket.send(Stopped(id=msg.id).model_dump_json())
                for t in tasks:
                    t.cancel()

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    sink_factory=sink_factory,
                    loop=asyncio.get_event_loop(),
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.1)
                await ws.cancel()
                await asyncio.sleep(0.3)

                rec = sink_factory.last_sink.records
                assert not any(r[0] == "end" for r in rec)
                assert any(r[0] == "abort" for r in rec)

                await ws.close()

        _run_test(go())

    def test_token_query_param(self):
        async def go():
            received_tokens: list[str] = []

            async def handler(websocket):
                path = websocket.request.path
                import urllib.parse

                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
                received_tokens.append(qs.get("token", [""])[0])
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    token="secret&token#test",
                    loop=asyncio.get_event_loop(),
                )
                ok = await ws.connect()
                assert ok is True
                assert received_tokens[0] == "secret&token#test"
                await ws.close()

        _run_test(go())

    def test_default_profile_sent(self):
        async def go():
            received: list = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        received.append(msg)
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    loop=asyncio.get_event_loop(),
                    default_profile="gpt-sovits",
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.15)

                speaks = [m for m in received if isinstance(m, Speak)]
                assert speaks and speaks[0].profile == "gpt-sovits"

                # 明示 profile は default を上書き
                await ws.speak("テスト2", profile="other")
                await asyncio.sleep(0.15)
                speaks = [m for m in received if isinstance(m, Speak)]
                assert speaks[-1].profile == "other"

                await ws.close()

        _run_test(go())

    def test_speak_send_failure_rollback(self):
        async def go():
            sink_factory = FakeSinkFactory()

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    sink_factory=sink_factory,
                    loop=asyncio.get_event_loop(),
                )
                await ws.connect()

                async def failing_send(model):
                    raise ConnectionError("send failed")

                ws._send = failing_send
                await ws.speak("テスト")
                await asyncio.sleep(0.1)

                assert ws._active_id is None
                idle_events = [s for s in states if s[0] == "idle"]
                assert len(idle_events) >= 1

                await ws.close()

        _run_test(go())
