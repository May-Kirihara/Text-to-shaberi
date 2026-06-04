"""test_ws_textstream: WSClient の text-stream API（client側）テスト。

websockets.serve で fake サーバを立て、start_text_stream/send_text_segment/
end_text_stream の送信列と、空ストリーム(AudioBeginなし)での idle 復帰(§14.1)を検証。
pytest-asyncio 不使用・asyncio.run。
"""
from __future__ import annotations

import asyncio
import json

import websockets

from text_to_shaberi.client.ws_client import WSClient
from text_to_shaberi.common.protocol import (
    AudioBegin,
    AudioEnd,
    AudioFormatPayload,
    Hello,
    Ready,
    SegText,
    StreamBegin,
    StreamEnd,
    parse_message,
)


class FakeEngine:
    def __init__(self, **kw):
        self.records = []

    def prewarm(self, device):
        self.records.append(("prewarm",))

    def begin(self, fmt, device):
        self.records.append(("begin",))

    def feed(self, chunk):
        self.records.append(("feed", len(chunk)))
        return True

    def end(self):
        self.records.append(("end",))

    def abort(self):
        self.records.append(("abort",))

    def close(self):
        self.records.append(("close",))

    def join(self, timeout=2.0):
        pass


class FakeEngineFactory:
    def __init__(self):
        self.engine = None

    def __call__(self, **kw):
        if self.engine is None:
            self.engine = FakeEngine(**kw)
        return self.engine


def _ws(port):
    return WSClient(
        f"ws://127.0.0.1:{port}/ws",
        on_log=lambda m: None,
        on_state=lambda e, p=None: None,
        engine_factory=FakeEngineFactory(),
        loop=asyncio.get_event_loop(),
        auto_reconnect=False,
    )


def test_text_stream_unsupported_returns_none():
    async def go():
        async def handler(websocket):
            async for raw in websocket:
                if isinstance(raw, str):
                    msg = parse_message(json.loads(raw))
                    if isinstance(msg, Hello):
                        await websocket.send(
                            Ready(session_id="t", features={"auto_tts": True}).model_dump_json()
                        )

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = _ws(port)
            await ws.connect()
            sid = await ws.start_text_stream()
            assert sid is None
            assert ws.is_busy() is False
            await ws.close()

    asyncio.run(go())


def test_text_stream_path_sends_begin_seg_end():
    async def go():
        received = []

        async def handler(websocket):
            async for raw in websocket:
                if isinstance(raw, str):
                    msg = parse_message(json.loads(raw))
                    received.append(msg)
                    if isinstance(msg, Hello):
                        await websocket.send(
                            Ready(
                                session_id="t",
                                features={"auto_tts": True, "text_stream": True},
                            ).model_dump_json()
                        )
                    elif isinstance(msg, StreamEnd):
                        await websocket.send(
                            AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                        )
                        await websocket.send(b"\x00\x00" * 100)
                        await websocket.send(AudioEnd(id=msg.id).model_dump_json())

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = _ws(port)
            await ws.connect()
            sid = await ws.start_text_stream()
            assert sid is not None
            await ws.send_text_segment(0, "こんにちは。")
            await ws.send_text_segment(1, "今日は晴れ。")
            await ws.end_text_stream()
            await asyncio.sleep(0.25)

            begins = [m for m in received if isinstance(m, StreamBegin)]
            segs = [m for m in received if isinstance(m, SegText)]
            ends = [m for m in received if isinstance(m, StreamEnd)]
            assert len(begins) == 1 and begins[0].mode == "text"
            assert [(s.index, s.text) for s in segs] == [(0, "こんにちは。"), (1, "今日は晴れ。")]
            assert len(ends) == 1
            assert ws.is_busy() is False
            await ws.close()

    asyncio.run(go())


def test_text_stream_empty_releases_on_audio_end():
    async def go():
        async def handler(websocket):
            async for raw in websocket:
                if isinstance(raw, str):
                    msg = parse_message(json.loads(raw))
                    if isinstance(msg, Hello):
                        await websocket.send(
                            Ready(
                                session_id="t",
                                features={"auto_tts": True, "text_stream": True},
                            ).model_dump_json()
                        )
                    elif isinstance(msg, StreamEnd):
                        await websocket.send(AudioEnd(id=msg.id).model_dump_json())

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = _ws(port)
            await ws.connect()
            sid = await ws.start_text_stream()
            assert sid is not None
            assert ws.is_busy() is True
            await ws.end_text_stream()
            await asyncio.sleep(0.25)
            assert ws.is_busy() is False
            await ws.close()

    asyncio.run(go())
