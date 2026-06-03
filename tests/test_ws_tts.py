"""S1 WS TTS テスト（GPT-SoVITS 不要・FakeTTSClient で単体検証）。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from text_to_shaberi.common.audio_format import AudioFormat
from text_to_shaberi.server.config import ServerConfig, TTSConfig, BindConfig
from text_to_shaberi.server.tts.profile import TTSProfile
from text_to_shaberi.server.ws_server import create_app


def _make_profile() -> TTSProfile:
    return TTSProfile(
        name="test",
        driver="generic_http",
        method="POST",
        url="http://localhost:9880/tts",
        body_type="json",
        timeout_sec=30,
        headers={},
        body={"text": "{{ text }}"},
        response_mode="audio_stream",
        response_content_type="audio/raw",
        audio=AudioFormat(encoding="pcm_s16le", sample_rate=32000, channels=1),
    )


class FakeTTSClient:
    """テスト用 TTSClient。stream() が b"\\x00\\x00"*N を複数チャンク yield する。"""

    def __init__(self, chunks: int = 3, chunk_samples: int = 160) -> None:
        self._chunks = chunks
        self._chunk_bytes = b"\x00\x00" * chunk_samples
        self.stream_called = False
        self.stream_profile = None
        self.stream_text = None

    async def stream(
        self, profile: TTSProfile, text: str, **variables: Any
    ) -> AsyncIterator[bytes]:
        self.stream_called = True
        self.stream_profile = profile
        self.stream_text = text
        for _ in range(self._chunks):
            yield self._chunk_bytes

    async def aclose(self) -> None:
        pass


class SlowFakeTTSClient(FakeTTSClient):
    """cancel 検証用: チャンク間で await asyncio.sleep する。"""

    def __init__(self, chunks: int = 10, chunk_samples: int = 160) -> None:
        super().__init__(chunks=chunks, chunk_samples=chunk_samples)

    async def stream(
        self, profile: TTSProfile, text: str, **variables: Any
    ) -> AsyncIterator[bytes]:
        self.stream_called = True
        self.stream_profile = profile
        self.stream_text = text
        for _ in range(self._chunks):
            await asyncio.sleep(0.1)
            yield self._chunk_bytes


def _make_config(**overrides: Any) -> ServerConfig:
    bind_kw = {k: v for k, v in overrides.items() if k in ("host", "port", "token")}
    return ServerConfig(
        bind=BindConfig(**bind_kw) if bind_kw else BindConfig(),
        tts=TTSConfig(
            profiles_dir="",
            active_profile="gpt-sovits",
        ),
    )


@pytest.fixture
def fake_client():
    return FakeTTSClient()


@pytest.fixture
def cfg():
    return _make_config()


class TestHelloReady:
    def test_hello_returns_ready(self, cfg, fake_client) -> None:
        with TestClient(create_app(cfg, tts_client=fake_client)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                data = ws.receive_json()
                assert data["type"] == "ready"
                assert data["codec"] == "pcm_s16le"
                assert "tts" in data
                assert data["tts"]["sample_rate"] == 32000
                assert data["tts"]["channels"] == 1
                assert isinstance(data["profiles"], list)
                assert data["features"]["auto_tts"] is False
                assert "server_time" in data
                assert data["limits"]["max_chunk_bytes"] == 65536

    def test_non_hello_first_message_closes(self, cfg, fake_client) -> None:
        with TestClient(create_app(cfg, tts_client=fake_client)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "speak", "id": "u1", "text": "hi"})
                data = ws.receive_json()
                assert data["type"] == "error"
                assert data["id"] is None


class TestSpeak:
    def test_speak_produces_audio_flow(self, cfg) -> None:
        fake = FakeTTSClient(chunks=3, chunk_samples=160)
        with TestClient(create_app(cfg, tts_client=fake)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                ready = ws.receive_json()
                assert ready["type"] == "ready"

                ws.send_json({"type": "speak", "id": "u1", "text": "テスト"})
                begin = ws.receive_json()
                assert begin["type"] == "audio_begin"
                assert begin["id"] == "u1"
                assert begin["format"]["codec"] == "pcm_s16le"

                audio_data = b""
                while True:
                    received = ws.receive()
                    if "bytes" in received and received.get("bytes"):
                        audio_data += received["bytes"]
                    elif "text" in received:
                        end_msg = json.loads(received["text"])
                        if end_msg["type"] == "audio_end":
                            assert end_msg["id"] == "u1"
                            break

                assert len(audio_data) > 0
                assert len(audio_data) == 160 * 2 * 3

    def test_speak_uses_injected_client(self, cfg) -> None:
        fake = FakeTTSClient(chunks=2)
        with TestClient(create_app(cfg, tts_client=fake)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                ws.receive_json()
                ws.send_json({"type": "speak", "id": "u1", "text": "hello"})
                ws.receive_json()
                for _ in range(2):
                    ws.receive()
                ws.receive_json()
        assert fake.stream_called
        assert fake.stream_text == "hello"


class TestBusySideband:
    def test_second_speak_while_active_is_busy_sideband(self, cfg) -> None:
        slow_fake = SlowFakeTTSClient(chunks=10)
        with TestClient(create_app(cfg, tts_client=slow_fake)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                ws.receive_json()

                ws.send_json({"type": "speak", "id": "u1", "text": "first"})
                ws.receive_json()

                ws.send_json({"type": "speak", "id": "u2", "text": "second"})
                busy = ws.receive_json()
                assert busy["type"] == "error"
                assert "busy" in busy["message"]
                assert busy["id"] == "u2"


class TestCancel:
    def test_cancel_stops_active_stream(self, cfg) -> None:
        slow_fake = SlowFakeTTSClient(chunks=20)
        with TestClient(create_app(cfg, tts_client=slow_fake)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                ws.receive_json()

                ws.send_json({"type": "speak", "id": "u1", "text": "cancel me"})
                ws.receive_json()

                import time
                time.sleep(0.15)

                ws.send_json({"type": "cancel", "id": "u1"})

                messages = []
                while True:
                    received = ws.receive()
                    if "text" in received:
                        msg = json.loads(received["text"])
                        messages.append(msg)
                        if msg["type"] in ("stopped", "error"):
                            break
                    elif "bytes" in received:
                        pass

                stopped = [m for m in messages if m["type"] == "stopped"]
                assert len(stopped) >= 1
                assert stopped[0]["id"] == "u1"

    def test_cancel_no_active_stream_returns_error(self, cfg, fake_client) -> None:
        with TestClient(create_app(cfg, tts_client=fake_client)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                ws.receive_json()

                ws.send_json({"type": "cancel", "id": "nonexist"})
                err = ws.receive_json()
                assert err["type"] == "error"
                assert "no active stream" in err["message"]


class TestToken:
    def test_wrong_token_rejected(self) -> None:
        fake = FakeTTSClient()
        cfg = _make_config(token="secret123")
        with TestClient(create_app(cfg, tts_client=fake)) as client:
            with pytest.raises(Exception):
                with client.websocket_connect("/ws?token=wrong") as ws:
                    ws.send_json({"type": "hello"})

    def test_correct_token_accepted(self) -> None:
        fake = FakeTTSClient()
        cfg = _make_config(token="secret123")
        with TestClient(create_app(cfg, tts_client=fake)) as client:
            with client.websocket_connect("/ws?token=secret123") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                data = ws.receive_json()
                assert data["type"] == "ready"

    def test_bearer_token_accepted(self) -> None:
        fake = FakeTTSClient()
        cfg = _make_config(token="secret123")
        with TestClient(create_app(cfg, tts_client=fake)) as client:
            with client.websocket_connect(
                "/ws", headers={"Authorization": "Bearer secret123"}
            ) as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                data = ws.receive_json()
                assert data["type"] == "ready"


class TestHandshakeErrors:
    def test_invalid_json_returns_error(self, cfg, fake_client) -> None:
        with TestClient(create_app(cfg, tts_client=fake_client)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_text("not json at all")
                data = ws.receive_json()
                assert data["type"] == "error"

    def test_unknown_type_returns_error_after_handshake(self, cfg, fake_client) -> None:
        with TestClient(create_app(cfg, tts_client=fake_client)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                ws.receive_json()
                ws.send_json({"type": "nonexistent_type"})
                err = ws.receive_json()
                assert err["type"] == "error"
                assert err["id"] is None


class TestSpeakIdRequired:
    def test_speak_empty_id_returns_error(self, cfg, fake_client) -> None:
        with TestClient(create_app(cfg, tts_client=fake_client)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello", "protocol_version": 1})
                ws.receive_json()
                ws.send_json({"type": "speak", "id": "", "text": "hi"})
                err = ws.receive_json()
                assert err["type"] == "error"
