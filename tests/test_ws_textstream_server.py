"""text-stream サーバ統合テスト（mode=text / seg_text / 空完了）。FakeTTS で GPU 不要。"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from text_to_shaberi.server.config import BindConfig, ServerConfig, STTConfig, TTSConfig
from text_to_shaberi.server.ws_server import create_app


class FakeTTSClient:
    def __init__(self, chunks: int = 3, chunk_samples: int = 160) -> None:
        self._chunks = chunks
        self._chunk_bytes = b"\x00\x00" * chunk_samples
        self.stream_texts: list[str] = []

    async def stream(self, profile: Any, text: str, **kw: Any) -> AsyncIterator[bytes]:
        self.stream_texts.append(text)
        for _ in range(self._chunks):
            yield self._chunk_bytes

    async def aclose(self) -> None:
        pass


class FailSTTEngine:
    def transcribe(self, samples, sample_rate, language):
        raise AssertionError("STT must not be called in text mode")


def _make_config() -> ServerConfig:
    return ServerConfig(
        bind=BindConfig(),
        stt=STTConfig(backend="qwen_local", language="ja"),
        tts=TTSConfig(profiles_dir="", active_profile="gpt-sovits"),
    )


def _do_hello(ws) -> dict:
    ws.send_json({"type": "hello", "protocol_version": 1})
    return ws.receive_json()


def _collect(ws, max_recv: int = 200):
    msgs = []
    audio = b""
    for _ in range(max_recv):
        received = ws.receive()
        if "bytes" in received and received.get("bytes"):
            audio += received["bytes"]
        elif "text" in received:
            m = json.loads(received["text"])
            msgs.append(m)
            if m["type"] in ("audio_end", "stream_stopped", "error"):
                break
    return msgs, audio


class TestTextStreamServer:
    def test_text_feature_advertised(self):
        cfg = _make_config()
        app = create_app(cfg, tts_client=FakeTTSClient(), stt_engine=FailSTTEngine())
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ready = _do_hello(ws)
                assert ready["features"]["text_stream"] is True

    def test_text_three_seg_single_audio_no_transcript(self):
        cfg = _make_config()
        tts = FakeTTSClient(chunks=3)
        app = create_app(cfg, tts_client=tts, stt_engine=FailSTTEngine())
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                sid = "t1"
                ws.send_json({"type": "stream_begin", "id": sid, "mode": "text", "auto_tts": True})
                for i, t in enumerate(["こんにちは。", "今日は。", "晴れ。"]):
                    ws.send_json({"type": "seg_text", "id": sid, "index": i, "text": t})
                ws.send_json({"type": "stream_end", "id": sid})
                msgs, audio = _collect(ws)
                begins = [m for m in msgs if m["type"] == "audio_begin"]
                ends = [m for m in msgs if m["type"] == "audio_end"]
                transcripts = [m for m in msgs if m["type"] == "transcript"]
                assert len(begins) == 1 and begins[0]["id"] == sid
                assert len(ends) == 1 and ends[0]["id"] == sid
                assert transcripts == []
                assert tts.stream_texts == ["こんにちは。", "今日は。", "晴れ。"]
                assert len(audio) > 0

    def test_empty_text_stream_still_sends_audio_end(self):
        cfg = _make_config()
        app = create_app(cfg, tts_client=FakeTTSClient(), stt_engine=FailSTTEngine())
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                sid = "t2"
                ws.send_json({"type": "stream_begin", "id": sid, "mode": "text", "auto_tts": True})
                ws.send_json({"type": "stream_end", "id": sid})
                msgs, _ = _collect(ws)
                ends = [m for m in msgs if m["type"] == "audio_end"]
                assert len(ends) == 1 and ends[0]["id"] == sid

    def test_seg_text_without_active_stream_errors(self):
        cfg = _make_config()
        app = create_app(cfg, tts_client=FakeTTSClient(), stt_engine=FailSTTEngine())
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                ws.send_json({"type": "seg_text", "id": "nope", "index": 0, "text": "x"})
                err = ws.receive_json()
                assert err["type"] == "error"

    def test_text_stream_explicit_profile_ok(self):
        cfg = _make_config()
        app = create_app(cfg, tts_client=FakeTTSClient(), stt_engine=FailSTTEngine())
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                sid = "t3"
                ws.send_json({"type": "stream_begin", "id": sid, "mode": "text", "auto_tts": True, "profile": "gpt-sovits"})
                ws.send_json({"type": "seg_text", "id": sid, "index": 0, "text": "やあ。"})
                ws.send_json({"type": "stream_end", "id": sid})
                msgs, audio = _collect(ws)
                ends = [m for m in msgs if m["type"] == "audio_end"]
                assert len(ends) == 1
                assert len(audio) > 0
