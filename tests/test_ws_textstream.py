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
    Cancel,
    Hello,
    Ready,
    SegText,
    StreamBegin,
    StreamEnd,
    StreamStopped,
    parse_message,
)


class FakeEngine:
    def __init__(self, **kw):
        self.records = []

    def prewarm(self, device):
        self.records.append(("prewarm",))

    def set_muted(self, muted):
        self.records.append(("set_muted", muted))

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


def _ws_audio(port, on_audio, engine_factory):
    return WSClient(
        f"ws://127.0.0.1:{port}/ws",
        on_log=lambda m: None,
        on_state=lambda e, p=None: None,
        engine_factory=engine_factory,
        loop=asyncio.get_event_loop(),
        auto_reconnect=False,
        on_audio=on_audio,
    )


def test_capture_taps_audio_and_suppresses_playback():
    async def go():
        events = []
        engine_factory = FakeEngineFactory()

        async def handler(websocket):
            async for raw in websocket:
                if isinstance(raw, str):
                    msg = parse_message(json.loads(raw))
                    if isinstance(msg, Hello):
                        await websocket.send(
                            Ready(
                                session_id="t",
                                codec="pcm_s16le",
                                tts=AudioFormatPayload(
                                    codec="pcm_s16le", sample_rate=16000, channels=1
                                ),
                                features={"auto_tts": True, "text_stream": True},
                            ).model_dump_json()
                        )
                    elif isinstance(msg, StreamEnd):
                        await websocket.send(
                            AudioBegin(
                                id=msg.id,
                                format=AudioFormatPayload(
                                    codec="pcm_s16le", sample_rate=16000, channels=1
                                ),
                            ).model_dump_json()
                        )
                        await websocket.send(b"\x11\x22" * 50)
                        await websocket.send(AudioEnd(id=msg.id).model_dump_json())

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = _ws_audio(
                port, lambda kind, payload: events.append((kind, payload)), engine_factory
            )
            await ws.connect()
            sid = await ws.start_text_stream(capture=True)
            assert sid is not None
            await ws.end_text_stream()
            await asyncio.sleep(0.25)

            assert [e[0] for e in events] == ["begin", "chunk", "end"]
            assert events[0][1] == {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}
            assert events[1][1] == b"\x11\x22" * 50
            assert events[2][1] is None
            # ローカル再生は行われない（engine.begin/feed/end 不発）
            rec = engine_factory.engine.records if engine_factory.engine else []
            assert not any(r[0] in ("begin", "feed", "end") for r in rec)
            assert ws.is_busy() is False
            await ws.close()

    asyncio.run(go())


def test_capture_emits_end_on_disconnect():
    async def go():
        events = []

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
                        await websocket.send(
                            AudioBegin(
                                id=msg.id,
                                format=AudioFormatPayload(
                                    codec="pcm_s16le", sample_rate=16000, channels=1
                                ),
                            ).model_dump_json()
                        )
                        await websocket.send(b"\x00\x00" * 10)
                        await websocket.close()  # audio_end を送らず途中切断

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = _ws_audio(
                port, lambda kind, payload: events.append(kind), FakeEngineFactory()
            )
            await ws.connect()
            await ws.start_text_stream(capture=True)
            await ws.end_text_stream()
            await asyncio.sleep(0.3)
            # 切断でも tap end が _finish_stream 経由で1回出る（§10.2）
            assert "begin" in events
            assert "end" in events
            await ws.close()

    asyncio.run(go())


def test_no_capture_plays_locally_and_no_tap():
    async def go():
        events = []
        engine_factory = FakeEngineFactory()

        async def handler(websocket):
            async for raw in websocket:
                if isinstance(raw, str):
                    msg = parse_message(json.loads(raw))
                    if isinstance(msg, Hello):
                        await websocket.send(
                            Ready(
                                session_id="t",
                                codec="pcm_s16le",
                                tts=AudioFormatPayload(
                                    codec="pcm_s16le", sample_rate=16000, channels=1
                                ),
                                features={"auto_tts": True, "text_stream": True},
                            ).model_dump_json()
                        )
                    elif isinstance(msg, StreamEnd):
                        await websocket.send(
                            AudioBegin(
                                id=msg.id,
                                format=AudioFormatPayload(
                                    codec="pcm_s16le", sample_rate=16000, channels=1
                                ),
                            ).model_dump_json()
                        )
                        await websocket.send(b"\x11\x22" * 20)
                        await websocket.send(AudioEnd(id=msg.id).model_dump_json())

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = _ws_audio(
                port, lambda kind, payload: events.append(kind), engine_factory
            )
            await ws.connect()
            await ws.start_text_stream(capture=False)  # capture 無効
            await ws.end_text_stream()
            await asyncio.sleep(0.25)
            assert events == []  # tap 未発火
            rec = engine_factory.engine.records
            assert any(r[0] == "begin" for r in rec)  # 通常どおりローカル再生
            assert any(r[0] == "feed" for r in rec)
            await ws.close()

    asyncio.run(go())


def test_capture_emits_end_on_stream_stopped():
    async def go():
        events = []

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
                        await websocket.send(
                            AudioBegin(
                                id=msg.id,
                                format=AudioFormatPayload(
                                    codec="pcm_s16le", sample_rate=16000, channels=1
                                ),
                            ).model_dump_json()
                        )
                        await websocket.send(b"\x00\x00" * 10)
                        # audio_end は送らず、client の cancel を待つ
                    elif isinstance(msg, Cancel):
                        await websocket.send(StreamStopped(id=msg.id).model_dump_json())

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws = _ws_audio(
                port, lambda kind, payload: events.append(kind), FakeEngineFactory()
            )
            await ws.connect()
            await ws.start_text_stream(capture=True)
            await ws.end_text_stream()
            await asyncio.sleep(0.15)
            assert "begin" in events
            await ws.cancel()
            await asyncio.sleep(0.2)
            # StreamStopped → _finish_stream → tap end が1回（§10.2）
            assert "end" in events
            await ws.close()

    asyncio.run(go())
