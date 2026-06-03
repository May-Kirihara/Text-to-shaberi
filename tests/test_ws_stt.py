"""S2 WS STT テスト（Qwen 不要・FakeSTTEngine で単体検証）。"""
from __future__ import annotations

import asyncio
import json
import struct
import threading
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from text_to_shaberi.common.audio_format import AudioFormat
from text_to_shaberi.server.config import BindConfig, ServerConfig, STTConfig, TTSConfig
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
    def __init__(self, chunks: int = 3, chunk_samples: int = 160) -> None:
        self._chunks = chunks
        self._chunk_bytes = b"\x00\x00" * chunk_samples

    async def stream(
        self, profile: TTSProfile, text: str, **variables: Any
    ) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            yield self._chunk_bytes

    async def aclose(self) -> None:
        pass


class FakeSTTEngine:
    def __init__(self, text: str = "こんにちは") -> None:
        self.text = text
        self.last_samples: np.ndarray | None = None
        self.last_sr: int | None = None
        self.last_language: str | None = None

    def transcribe(self, samples: np.ndarray, sample_rate: int, language: str) -> str:
        self.last_samples = samples
        self.last_sr = sample_rate
        self.last_language = language
        return self.text


class BlockingFakeSTTEngine(FakeSTTEngine):
    def __init__(self, text: str = "こんにちは", block_event: threading.Event | None = None) -> None:
        super().__init__(text=text)
        self._block_event = block_event or threading.Event()

    def transcribe(self, samples: np.ndarray, sample_rate: int, language: str) -> str:
        self._block_event.wait(timeout=5.0)
        return super().transcribe(samples, sample_rate, language)


def _make_config(**overrides: Any) -> ServerConfig:
    bind_kw = {k: v for k, v in overrides.items() if k in ("host", "port", "token")}
    return ServerConfig(
        bind=BindConfig(**bind_kw) if bind_kw else BindConfig(),
        stt=STTConfig(backend="qwen_local", language="ja"),
        tts=TTSConfig(profiles_dir="", active_profile="gpt-sovits"),
    )


def _make_pcm_s16le_mono(samples: int = 16000) -> bytes:
    return struct.pack(f"<{samples}h", *([16384] * samples))


def _ws_connect(client: TestClient):
    return client.websocket_connect("/ws")


def _do_hello(ws) -> dict:
    ws.send_json({"type": "hello", "protocol_version": 1})
    return ws.receive_json()


@pytest.fixture
def cfg():
    return _make_config()


@pytest.fixture
def fake_tts():
    return FakeTTSClient()


@pytest.fixture
def fake_stt():
    return FakeSTTEngine()


class TestSTTBasic:
    def test_stt_no_auto_tts(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws)
                assert ready["features"]["auto_tts"] is True

                pcm = _make_pcm_s16le_mono(16000)
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_bytes(pcm)
                ws.send_json({"type": "stt_end", "id": "u2"})

                transcript = ws.receive_json()
                assert transcript["type"] == "transcript"
                assert transcript["id"] == "u2"
                assert transcript["text"] == "こんにちは"
                assert transcript["final"] is True

    def test_stt_with_auto_tts(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                pcm = _make_pcm_s16le_mono(16000)
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": True,
                })
                ws.send_bytes(pcm)
                ws.send_json({"type": "stt_end", "id": "u2"})

                transcript = ws.receive_json()
                assert transcript["type"] == "transcript"
                assert transcript["id"] == "u2"

                begin = ws.receive_json()
                assert begin["type"] == "audio_begin"
                assert begin["id"] == "tts-u2"
                assert begin["parent_id"] == "u2"

                audio_data = b""
                while True:
                    received = ws.receive()
                    if "bytes" in received and received.get("bytes"):
                        audio_data += received["bytes"]
                    elif "text" in received:
                        end_msg = json.loads(received["text"])
                        if end_msg["type"] == "audio_end":
                            assert end_msg["id"] == "tts-u2"
                            break

                assert len(audio_data) > 0


class TestSTTBusy:
    def test_stt_begin_while_capture_is_busy(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({
                    "type": "stt_begin",
                    "id": "u1",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })

                busy = ws.receive_json()
                assert busy["type"] == "error"
                assert "busy" in busy["message"]
                assert busy["id"] == "u2"

                ws.send_bytes(_make_pcm_s16le_mono(100))
                ws.send_json({"type": "stt_end", "id": "u1"})
                transcript = ws.receive_json()
                assert transcript["type"] == "transcript"
                assert transcript["id"] == "u1"

    def test_speak_while_capture_is_busy(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({
                    "type": "stt_begin",
                    "id": "u1",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_json({"type": "speak", "id": "s1", "text": "hello"})

                busy = ws.receive_json()
                assert busy["type"] == "error"
                assert "busy" in busy["message"]
                assert busy["id"] == "s1"


class TestSTTErrors:
    def test_binary_without_active_stt(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_bytes(b"\x00\x00" * 100)
                err = ws.receive_json()
                assert err["type"] == "error"
                assert "no active stt stream" in err["message"]

    def test_stt_end_id_mismatch_sideband(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({
                    "type": "stt_begin",
                    "id": "u1",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_json({"type": "stt_end", "id": "wrong"})

                err = ws.receive_json()
                assert err["type"] == "error"
                assert err["id"] == "wrong"
                assert "no active stt stream" in err["message"]

                ws.send_bytes(_make_pcm_s16le_mono(100))
                ws.send_json({"type": "stt_end", "id": "u1"})
                transcript = ws.receive_json()
                assert transcript["type"] == "transcript"
                assert transcript["id"] == "u1"

    def test_stt_begin_no_id(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({
                    "type": "stt_begin",
                    "id": "",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                err = ws.receive_json()
                assert err["type"] == "error"
                assert "stt_begin.id is required" in err["message"]


class TestSTTCancel:
    def test_cancel_capture(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_json({"type": "cancel", "id": "u2"})

                stopped = ws.receive_json()
                assert stopped["type"] == "stopped"
                assert stopped["id"] == "u2"

    def test_cancel_during_stt_inference(self, cfg, fake_tts) -> None:
        block_event = threading.Event()
        blocking_stt = BlockingFakeSTTEngine(block_event=block_event)
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=blocking_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                pcm = _make_pcm_s16le_mono(16000)
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_bytes(pcm)
                ws.send_json({"type": "stt_end", "id": "u2"})

                import time
                time.sleep(0.1)

                ws.send_json({"type": "cancel", "id": "u2"})
                block_event.set()

                messages = []
                while True:
                    received = ws.receive()
                    if "text" in received:
                        msg = json.loads(received["text"])
                        messages.append(msg)
                        if msg["type"] in ("stopped", "error", "transcript"):
                            break

                stopped = [m for m in messages if m["type"] == "stopped"]
                assert len(stopped) >= 1
                assert stopped[0]["id"] == "u2"

    def test_cancel_auto_tts_tts_phase(self, cfg, fake_stt) -> None:
        class SlowFakeTTSClient(FakeTTSClient):
            async def stream(self, profile, text, **kw):
                for _ in range(20):
                    await asyncio.sleep(0.1)
                    yield self._chunk_bytes

        slow_tts = SlowFakeTTSClient(chunks=20)
        with TestClient(create_app(cfg, tts_client=slow_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                pcm = _make_pcm_s16le_mono(16000)
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": True,
                })
                ws.send_bytes(pcm)
                ws.send_json({"type": "stt_end", "id": "u2"})

                transcript = ws.receive_json()
                assert transcript["type"] == "transcript"

                import time
                time.sleep(0.15)

                ws.send_json({"type": "cancel", "id": "u2"})

                messages = []
                while True:
                    received = ws.receive()
                    if "text" in received:
                        msg = json.loads(received["text"])
                        messages.append(msg)
                        if msg["type"] in ("stopped", "error"):
                            break

                stopped = [m for m in messages if m["type"] == "stopped"]
                assert len(stopped) >= 1
                assert stopped[0]["id"] == "tts-u2"

    def test_cancel_no_match_returns_error(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({"type": "cancel", "id": "nonexist"})
                err = ws.receive_json()
                assert err["type"] == "error"
                assert "no active stream" in err["message"]


class TestSTTEdgeCases:
    def test_empty_audio_transcript_empty(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_json({"type": "stt_end", "id": "u2"})

                transcript = ws.receive_json()
                assert transcript["type"] == "transcript"
                assert transcript["text"] == ""
                assert transcript["final"] is True

    def test_empty_audio_auto_tts_no_tts(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": True,
                })
                ws.send_json({"type": "stt_end", "id": "u2"})

                transcript = ws.receive_json()
                assert transcript["text"] == ""

    def test_partial_frame_bytes_truncated(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                full_pcm = _make_pcm_s16le_mono(100)
                partial = full_pcm + b"\x00"

                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_bytes(partial)
                ws.send_json({"type": "stt_end", "id": "u2"})

                transcript = ws.receive_json()
                assert transcript["type"] == "transcript"
                assert transcript["text"] == "こんにちは"

                assert fake_stt.last_samples is not None
                assert len(fake_stt.last_samples) == 100

    def test_stt_engine_receives_correct_format(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                pcm = _make_pcm_s16le_mono(8000)
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_bytes(pcm)
                ws.send_json({"type": "stt_end", "id": "u2"})

                ws.receive_json()

                assert fake_stt.last_sr == 16000
                assert fake_stt.last_language == "ja"
                assert fake_stt.last_samples is not None
                assert fake_stt.last_samples.dtype == np.float32


class TestSTTSpeakRegression:
    def test_speak_still_works(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({"type": "speak", "id": "u1", "text": "テスト"})
                begin = ws.receive_json()
                assert begin["type"] == "audio_begin"
                assert begin["id"] == "u1"
                assert begin["parent_id"] is None

                audio_data = b""
                while True:
                    received = ws.receive()
                    if "bytes" in received and received.get("bytes"):
                        audio_data += received["bytes"]
                    elif "text" in received:
                        end_msg = json.loads(received["text"])
                        if end_msg["type"] == "audio_end":
                            break

                assert len(audio_data) > 0

    def test_speak_audio_begin_has_no_parent_id(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                ws.send_json({"type": "speak", "id": "u1", "text": "hi"})
                begin = ws.receive_json()
                assert begin["parent_id"] is None

                while True:
                    received = ws.receive()
                    if "text" in received:
                        msg = json.loads(received["text"])
                        if msg["type"] == "audio_end":
                            break


class TestSTTReadyFeatures:
    def test_ready_features_auto_tts_true(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws)
                assert ready["features"]["auto_tts"] is True


class TestSTTInjection:
    def test_injected_stt_engine_used(self, cfg, fake_tts, fake_stt) -> None:
        with TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)) as client:
            with _ws_connect(client) as ws:
                _do_hello(ws)

                pcm = _make_pcm_s16le_mono(100)
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u2",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                ws.send_bytes(pcm)
                ws.send_json({"type": "stt_end", "id": "u2"})

                transcript = ws.receive_json()
                assert transcript["text"] == "こんにちは"
                assert fake_stt.last_samples is not None
