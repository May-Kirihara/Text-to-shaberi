"""test_ws_client: WSClient の fake WS サーバによるテスト。

pytest-asyncio 不使用。asyncio.run で完結。
websockets.serve で fake サーバを立てる。
C1: speak/side-band/terminal/cancel/token/default_profile/rollback。
C2: STT送信/自動tts連鎖/空文字起こし/エラー/キャンセル/atomic送信/自動tts降格。
X1: opus decode 経路 / sink begin codec==pcm_s16le / decode 例外 terminal。
"""
from __future__ import annotations

import asyncio
import json
import struct
import time

import pytest
import websockets

from text_to_shaberi.client.ws_client import WSClient
from text_to_shaberi.common.opus_codec import (
    OPUS_CODEC_NAME,
    OpusStreamEncoder,
    opus_available,
    opus_wire_format,
)
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
    SttBegin,
    SttEnd,
    Transcript,
    parse_message,
)


class FakeSink:
    def __init__(self, **kwargs):
        self.records: list[tuple] = []
        self._loop = kwargs.get("loop")
        self._on_log = kwargs.get("on_log")
        self.begin_fmt = None

    def set_muted(self, muted):
        self.records.append(("set_muted", muted))

    def begin(self, fmt, device):
        self.begin_fmt = fmt
        self.records.append(("begin", fmt.sample_rate, device, fmt.codec))

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


class FakeEngine:
    def __init__(self, **kwargs):
        self.records: list[tuple] = []
        self._loop = kwargs.get("loop")
        self._on_log = kwargs.get("on_log")
        self.begin_fmt = None
        self._prewarmed = False

    def prewarm(self, device):
        self._prewarmed = True
        self.records.append(("prewarm", device))

    def set_muted(self, muted):
        self.records.append(("set_muted", muted))

    def begin(self, fmt, device):
        self.begin_fmt = fmt
        self.records.append(("begin", fmt.sample_rate, device, fmt.codec))

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
        self.engine: FakeEngine | None = None

    def __call__(self, **kwargs):
        if self.engine is None:
            self.engine = FakeEngine(**kwargs)
        return self.engine


def _run_test(coroutine):
    return asyncio.run(coroutine)


class TestWSClient:
    def test_connect_and_speak(self):
        async def go():
            engine_factory = FakeEngineFactory()
            received: list = []

            ready_msg = Ready(
                session_id="test",
                codec="pcm_s16le",
                tts=AudioFormatPayload(codec="pcm_s16le", sample_rate=32000, channels=1),
                profiles=[],
            )

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        received.append(msg)
                        if isinstance(msg, Hello):
                            await websocket.send(ready_msg.model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(
                                AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                            )
                            await websocket.send(b"\x00\x00" * 100)
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                ok = await ws.connect()
                assert ok is True

                await ws.speak("テスト")
                await asyncio.sleep(0.2)

                assert engine_factory.engine is not None
                rec = engine_factory.engine.records
                assert any(r[0] == "begin" for r in rec)
                assert any(r[0] == "feed" for r in rec)
                assert any(r[0] == "end" for r in rec)

                await ws.close()

        _run_test(go())

    def test_sideband_error_continues(self):
        async def go():
            engine_factory = FakeEngineFactory()

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

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.3)

                rec = engine_factory.engine.records
                assert not any(r[0] == "abort" for r in rec)

                await ws.close()

        _run_test(go())

    def test_terminal_error_aborts(self):
        async def go():
            engine_factory = FakeEngineFactory()

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

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.2)

                if engine_factory.engine is not None:
                    rec = engine_factory.engine.records
                    assert any(r[0] == "abort" for r in rec)

                await ws.close()

        _run_test(go())

    def test_cancel_discards_binary_and_no_end(self):
        async def go():
            engine_factory = FakeEngineFactory()
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

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.1)
                await ws.cancel()
                await asyncio.sleep(0.3)

                rec = engine_factory.engine.records
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
                    auto_reconnect=False,
                    token_in_query=True,
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
                            await websocket.send(
                                AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                            )
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                    default_profile="gpt-sovits",
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.15)

                speaks = [m for m in received if isinstance(m, Speak)]
                assert speaks and speaks[0].profile == "gpt-sovits"

                await ws.speak("テスト2", profile="other")
                await asyncio.sleep(0.15)
                speaks = [m for m in received if isinstance(m, Speak)]
                assert speaks[-1].profile == "other"

                await ws.close()

        _run_test(go())

    def test_speak_send_failure_rollback(self):
        async def go():
            engine_factory = FakeEngineFactory()

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
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
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


class TestWSClientSTT:
    def test_utter_sends_stt_begin_binary_end(self):
        async def go():
            received_texts: list[str] = []
            received_binaries: list[bytes] = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, bytes):
                        received_binaries.append(raw)
                        continue
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        received_texts.append(raw)
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            stt_id = msg.id
                            await websocket.send(Transcript(id=stt_id, text="こんにちは", final=True).model_dump_json())
                            tts_id = f"tts-{stt_id}"
                            await websocket.send(AudioBegin(
                                id=tts_id, parent_id=stt_id,
                                format=AudioFormatPayload(),
                            ).model_dump_json())
                            await websocket.send(b"\x00\x00" * 100)
                            await websocket.send(AudioEnd(id=tts_id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                engine_factory = FakeEngineFactory()
                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                pcm = b"\x00\x01" * 1000
                await ws.utter(pcm, auto_tts=True)
                await asyncio.sleep(0.5)

                stt_begins = [json.loads(t) for t in received_texts if json.loads(t).get("type") == "stt_begin"]
                assert len(stt_begins) == 1
                assert stt_begins[0]["format"]["channels"] == 1
                assert stt_begins[0]["auto_tts"] is True
                assert len(received_binaries) > 0

                stt_ends = [json.loads(t) for t in received_texts if json.loads(t).get("type") == "stt_end"]
                assert len(stt_ends) == 1

                assert ws._active_id is None
                transcript_states = [s for s in states if s[0] == "transcript"]
                assert len(transcript_states) == 1
                assert transcript_states[0][1] == "こんにちは"

                assert engine_factory.engine is not None
                rec = engine_factory.engine.records
                assert any(r[0] == "begin" for r in rec)
                assert any(r[0] == "feed" for r in rec)
                assert any(r[0] == "end" for r in rec)

                await ws.close()

        _run_test(go())

    def test_utter_empty_transcript_no_audio(self):
        async def go():
            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            await websocket.send(Transcript(id=msg.id, text="", final=True).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                engine_factory = FakeEngineFactory()
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.3)

                assert ws._active_id is None
                idle_events = [s for s in states if s[0] == "idle"]
                assert len(idle_events) >= 1

                await ws.close()

        _run_test(go())

    def test_auto_tts_error_terminal(self):
        async def go():
            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            stt_id = msg.id
                            await websocket.send(Transcript(id=stt_id, text="test", final=True).model_dump_json())
                            tts_id = f"tts-{stt_id}"
                            await websocket.send(AudioBegin(
                                id=tts_id, parent_id=stt_id,
                                format=AudioFormatPayload(),
                            ).model_dump_json())
                            await websocket.send(ErrorMsg(id=tts_id, message="TTS failed").model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                engine_factory = FakeEngineFactory()
                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.3)

                assert ws._active_id is None
                assert any("TTS失敗" in l for l in logs)
                error_states = [s for s in states if s[0] == "error"]
                assert len(error_states) >= 1

                await ws.close()

        _run_test(go())

    def test_stt_error_terminal(self):
        async def go():
            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            await websocket.send(ErrorMsg(id=msg.id, message="STT failed").model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                engine_factory = FakeEngineFactory()
                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.3)

                assert ws._active_id is None
                assert any("STT/プロファイル失敗" in l for l in logs)

                await ws.close()

        _run_test(go())

    def test_capture_reset_error_then_next_utter(self):
        async def go():
            utter_count = [0]

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttBegin):
                            pass
                        elif isinstance(msg, SttEnd):
                            utter_count[0] += 1
                            if utter_count[0] == 1:
                                await websocket.send(ErrorMsg(
                                    id=msg.id, message="stt audio too large",
                                ).model_dump_json())
                            else:
                                await websocket.send(Transcript(
                                    id=msg.id, text="OK", final=True,
                                ).model_dump_json())
                    elif isinstance(raw, bytes):
                        pass

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                logs: list[str] = []
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()

                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.3)

                assert ws._active_id is None

                await ws.utter(b"\x00" * 500, auto_tts=True)
                await asyncio.sleep(0.3)

                assert utter_count[0] == 2

                await ws.close()

        _run_test(go())

    def test_channels_always_1(self):
        async def go():
            received_texts: list[str] = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        received_texts.append(raw)
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            await websocket.send(Transcript(id=msg.id, text="t", final=True).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                    stt_format=AudioFormatPayload(codec="pcm_s16le", sample_rate=16000, channels=2),
                )
                await ws.connect()
                await ws.utter(b"\x00" * 1000, auto_tts=False)
                await asyncio.sleep(0.2)

                stt_begins = [json.loads(t) for t in received_texts if json.loads(t).get("type") == "stt_begin"]
                assert len(stt_begins) == 1
                assert stt_begins[0]["format"]["channels"] == 1

                await ws.close()

        _run_test(go())

    def test_auto_tts_degradation(self):
        async def go():
            received_texts: list[str] = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        received_texts.append(raw)
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())
                        elif isinstance(msg, SttEnd):
                            await websocket.send(Transcript(id=msg.id, text="t", final=True).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                logs: list[str] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: None,
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.2)

                stt_begins = [json.loads(t) for t in received_texts if json.loads(t).get("type") == "stt_begin"]
                assert len(stt_begins) == 1
                assert stt_begins[0]["auto_tts"] is False
                assert any("auto_tts 非対応" in l for l in logs)

                await ws.close()

        _run_test(go())

    def test_cancel_sends_active_id_not_tts_id(self):
        async def go():
            received: list = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        received.append(msg)
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            stt_id = msg.id
                            await websocket.send(Transcript(id=stt_id, text="hi", final=True).model_dump_json())
                            tts_id = f"tts-{stt_id}"
                            await websocket.send(AudioBegin(
                                id=tts_id, parent_id=stt_id,
                                format=AudioFormatPayload(),
                            ).model_dump_json())
                        elif isinstance(msg, Cancel):
                            await websocket.send(Stopped(id=f"tts-{msg.id}").model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.3)
                await ws.cancel()
                await asyncio.sleep(0.3)

                cancels = [m for m in received if isinstance(m, Cancel)]
                assert len(cancels) == 1
                assert not cancels[0].id.startswith("tts-")

                assert ws._active_id is None

                await ws.close()

        _run_test(go())

    def test_utter_send_failure_rollback(self):
        async def go():
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
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()

                async def failing_send(payload):
                    raise ConnectionError("send failed")

                ws._ws_send = failing_send
                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.1)

                assert ws._active_id is None
                idle_events = [s for s in states if s[0] == "idle"]
                assert len(idle_events) >= 1

                await ws.close()

        _run_test(go())

    def test_utter_atomic_send_cancel_after_stt_end(self):
        async def go():
            received: list = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        received.append(msg)
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            stt_id = msg.id
                            await websocket.send(Transcript(id=stt_id, text="", final=True).model_dump_json())
                        elif isinstance(msg, Cancel):
                            await websocket.send(Stopped(id=msg.id).model_dump_json())
                    elif isinstance(raw, bytes):
                        pass

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                states: list[tuple] = []

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()

                pcm = b"\x00" * 50000
                await ws.utter(pcm, auto_tts=True)
                await asyncio.sleep(0.5)

                stt_ends = [m for m in received if isinstance(m, SttEnd)]
                cancels = [m for m in received if isinstance(m, Cancel)]

                assert len(stt_ends) == 1

                if cancels:
                    stt_end_idx = next(
                        i for i, m in enumerate(received) if isinstance(m, SttEnd)
                    )
                    cancel_idx = next(
                        i for i, m in enumerate(received) if isinstance(m, Cancel)
                    )
                    assert cancel_idx > stt_end_idx

                assert ws._active_id is None

                await ws.close()

        _run_test(go())


class TestWSClientOpus:
    @pytest.mark.skipif(not opus_available(), reason="opuslib not available")
    def test_opus_decode_feeds_pcm_to_sink(self) -> None:
        async def go():
            engine_factory = FakeEngineFactory()

            pcm_32k = b"\x00\x00" * 32000
            enc = OpusStreamEncoder(src_rate=32000, bitrate=24000)
            opus_packets = enc.feed(pcm_32k) + enc.flush()
            assert len(opus_packets) > 0

            opus_fmt = opus_wire_format()

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                codec=OPUS_CODEC_NAME,
                                tts=AudioFormatPayload(codec="pcm_s16le", sample_rate=32000, channels=1),
                                profiles=[],
                            ).model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(
                                AudioBegin(id=msg.id, format=opus_fmt).model_dump_json()
                            )
                            for pkt in opus_packets:
                                await websocket.send(pkt)
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                    codec_pref="opus",
                )
                ok = await ws.connect()
                assert ok is True

                await ws.speak("テスト")
                await asyncio.sleep(0.5)

                assert engine_factory.engine is not None
                engine = engine_factory.engine
                assert engine.begin_fmt is not None
                assert engine.begin_fmt.codec == "pcm_s16le"
                assert engine.begin_fmt.sample_rate == 48000

                feeds = [r for r in engine.records if r[0] == "feed"]
                assert len(feeds) > 0
                for f in feeds:
                    assert f[1] == 960 * 2

                assert any(r[0] == "end" for r in engine.records)

                await ws.close()

        _run_test(go())

    @pytest.mark.skipif(not opus_available(), reason="opuslib not available")
    def test_opus_decode_exception_terminal(self) -> None:
        async def go():
            engine_factory = FakeEngineFactory()
            states: list[tuple] = []
            logs: list[str] = []

            opus_fmt = opus_wire_format()

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                codec=OPUS_CODEC_NAME,
                                profiles=[],
                            ).model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(
                                AudioBegin(id=msg.id, format=opus_fmt).model_dump_json()
                            )
                            await websocket.send(b"\xff\xff\xff\xff\xff\xff")
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=logs.append,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                    codec_pref="opus",
                )
                ok = await ws.connect()
                assert ok is True

                await ws.speak("テスト")
                await asyncio.sleep(0.5)

                assert ws._active_id is None
                error_states = [s for s in states if s[0] == "error"]
                assert len(error_states) >= 1

                await ws.close()

        _run_test(go())

    def test_pcm_regression_unchanged(self) -> None:
        async def go():
            engine_factory = FakeEngineFactory()

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                codec="pcm_s16le",
                                tts=AudioFormatPayload(codec="pcm_s16le", sample_rate=32000, channels=1),
                                profiles=[],
                            ).model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(
                                AudioBegin(
                                    id=msg.id,
                                    format=AudioFormatPayload(codec="pcm_s16le", sample_rate=32000, channels=1),
                                ).model_dump_json()
                            )
                            await websocket.send(b"\x00\x00" * 100)
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                    codec_pref="pcm",
                )
                ok = await ws.connect()
                assert ok is True

                await ws.speak("テスト")
                await asyncio.sleep(0.3)

                assert engine_factory.engine is not None
                engine = engine_factory.engine
                assert engine.begin_fmt is not None
                assert engine.begin_fmt.codec == "pcm_s16le"
                assert engine.begin_fmt.sample_rate == 32000

                feeds = [r for r in engine.records if r[0] == "feed"]
                assert len(feeds) > 0

                await ws.close()

        _run_test(go())


class TestWSClientMetrics:
    def test_speak_metrics_emitted(self):
        async def go():
            engine_factory = FakeEngineFactory()
            states: list[tuple] = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                codec="pcm_s16le",
                                tts=AudioFormatPayload(codec="pcm_s16le", sample_rate=32000, channels=1),
                                profiles=[],
                            ).model_dump_json())
                        elif isinstance(msg, Speak):
                            await websocket.send(
                                AudioBegin(id=msg.id, format=AudioFormatPayload()).model_dump_json()
                            )
                            await websocket.send(b"\x00\x00" * 100)
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                ok = await ws.connect()
                assert ok is True

                await ws.speak("テスト")
                await asyncio.sleep(0.3)

                metrics_events = [s for s in states if s[0] == "metrics"]
                assert len(metrics_events) >= 2

                first_metrics = metrics_events[0]
                assert first_metrics[1]["kind"] == "speak"
                assert first_metrics[1]["ttfa_ms"] is None
                assert first_metrics[1]["stt_ms"] is None
                assert first_metrics[1]["codec"] == "pcm_s16le"

                ttfa_events = [m for m in metrics_events if m[1].get("ttfa_ms") is not None]
                assert len(ttfa_events) >= 1
                assert ttfa_events[0][1]["kind"] == "speak"
                assert ttfa_events[0][1]["ttfa_ms"] > 0

                await ws.close()

        _run_test(go())

    def test_utter_metrics_with_stt_ms(self):
        async def go():
            engine_factory = FakeEngineFactory()
            states: list[tuple] = []

            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, bytes):
                        pass
                    elif isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(
                                session_id="t",
                                features={"auto_tts": True},
                            ).model_dump_json())
                        elif isinstance(msg, SttEnd):
                            stt_id = msg.id
                            await websocket.send(Transcript(id=stt_id, text="テスト", final=True).model_dump_json())
                            tts_id = f"tts-{stt_id}"
                            await websocket.send(AudioBegin(
                                id=tts_id, parent_id=stt_id,
                                format=AudioFormatPayload(),
                            ).model_dump_json())
                            await websocket.send(b"\x00\x00" * 100)
                            await websocket.send(AudioEnd(id=tts_id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=engine_factory,
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.utter(b"\x00" * 1000, auto_tts=True)
                await asyncio.sleep(0.5)

                metrics_events = [s for s in states if s[0] == "metrics"]
                assert len(metrics_events) >= 2

                first_metrics = metrics_events[0]
                assert first_metrics[1]["kind"] == "utter"
                assert first_metrics[1]["ttfa_ms"] is None
                assert first_metrics[1]["stt_ms"] is None

                stt_events = [m for m in metrics_events if m[1].get("stt_ms") is not None]
                assert len(stt_events) >= 1
                assert stt_events[0][1]["stt_ms"] > 0

                await ws.close()

        _run_test(go())

    def test_speak_metrics_stt_ms_is_none(self):
        async def go():
            states: list[tuple] = []

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
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.speak("テスト")
                await asyncio.sleep(0.3)

                metrics_events = [s for s in states if s[0] == "metrics"]
                for m in metrics_events:
                    assert m[1]["kind"] == "speak"
                    assert m[1]["stt_ms"] is None

                await ws.close()

        _run_test(go())

    def test_metrics_cleared_on_stream_start(self):
        async def go():
            states: list[tuple] = []

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
                            await websocket.send(AudioEnd(id=msg.id).model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: states.append((e, p)),
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()
                await ws.speak("first")
                await asyncio.sleep(0.3)

                metrics_events = [s for s in states if s[0] == "metrics"]
                first_clear = metrics_events[0]
                assert first_clear[1]["ttfa_ms"] is None
                assert first_clear[1]["stt_ms"] is None

                await ws.close()

        _run_test(go())

    def test_apply_server_settings(self):
        async def go():
            async def handler(websocket):
                async for raw in websocket:
                    if isinstance(raw, str):
                        msg = parse_message(json.loads(raw))
                        if isinstance(msg, Hello):
                            await websocket.send(Ready(session_id="t").model_dump_json())

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                ws = WSClient(
                    f"ws://127.0.0.1:{port}/ws",
                    on_log=lambda m: None,
                    on_state=lambda e, p=None: None,
                    engine_factory=FakeEngineFactory(),
                    loop=asyncio.get_event_loop(),
                    auto_reconnect=False,
                )
                await ws.connect()

                ws._apply_server_settings(
                    codec_pref="pcm",
                    tls_insecure=True,
                    tls_cafile="/path",
                    token="new_token",
                )

                assert ws._codec_pref == "pcm"
                assert ws._tls_insecure is True
                assert ws._tls_cafile == "/path"
                assert ws._token == "new_token"

                await ws.close()

        _run_test(go())
