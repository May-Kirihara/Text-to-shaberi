"""speak_stream WS 統合テスト（§5.1）。FakeTTS/STT で GPU/マイク不要。"""
from __future__ import annotations

import asyncio
import json
import struct
import threading
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from text_to_shaberi.server.config import BindConfig, ServerConfig, STTConfig, TTSConfig
from text_to_shaberi.server.tts.profile import TTSProfile
from text_to_shaberi.server.ws_server import create_app


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeTTSClient:
    def __init__(self, chunks: int = 3, chunk_samples: int = 160, delay: float = 0.0) -> None:
        self._chunks = chunks
        self._chunk_bytes = b"\x00\x00" * chunk_samples
        self._delay = delay
        self.stream_texts: list[str] = []

    async def stream(self, profile: TTSProfile, text: str, **kw: Any) -> AsyncIterator[bytes]:
        self.stream_texts.append(text)
        for _ in range(self._chunks):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield self._chunk_bytes

    async def aclose(self) -> None:
        pass


class SlowFakeTTSClient(FakeTTSClient):
    def __init__(self, chunks: int = 10, chunk_samples: int = 160) -> None:
        super().__init__(chunks=chunks, chunk_samples=chunk_samples, delay=0.05)


class IndexFakeSTTEngine:
    def __init__(self, texts: list[str] | None = None) -> None:
        self._texts = texts or ["seg0", "seg1", "seg2"]
        self._idx = 0

    def transcribe(self, samples: np.ndarray, sample_rate: int, language: str) -> str:
        text = self._texts[self._idx % len(self._texts)]
        self._idx += 1
        return text


class BlockingFakeSTTEngine:
    def __init__(self, text: str = "blocked", block_event: threading.Event | None = None) -> None:
        self.text = text
        self._block_event = block_event or threading.Event()

    def transcribe(self, samples: np.ndarray, sample_rate: int, language: str) -> str:
        self._block_event.wait(timeout=5.0)
        return self.text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_config() -> ServerConfig:
    return ServerConfig(
        bind=BindConfig(),
        stt=STTConfig(backend="qwen_local", language="ja"),
        tts=TTSConfig(profiles_dir="", active_profile="gpt-sovits"),
    )


def _make_pcm(samples: int = 16000) -> bytes:
    return struct.pack(f"<{samples}h", *([16384] * samples))


def _do_hello(ws) -> dict:
    ws.send_json({"type": "hello", "protocol_version": 1})
    return ws.receive_json()


def _collect(ws, max_recv: int = 200):
    msgs: list[dict] = []
    audio_data = b""
    for _ in range(max_recv):
        received = ws.receive()
        if "bytes" in received and received.get("bytes"):
            audio_data += received["bytes"]
        elif "text" in received:
            msg = json.loads(received["text"])
            msgs.append(msg)
            if msg["type"] in ("audio_end", "stream_stopped"):
                break
    return msgs, audio_data


def _send_seg(ws, sid: str, index: int, pcm: bytes) -> None:
    ws.send_json({"type": "seg_begin", "id": sid, "index": index})
    ws.send_bytes(pcm)
    ws.send_json({"type": "seg_end", "id": sid, "index": index})


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestStreamBasic:
    def test_three_seg_single_audio(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = FakeTTSClient(chunks=3)
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                ready = _do_hello(ws)
                assert ready["features"]["speak_stream"] is True

                sid = "stream1"
                ws.send_json({"type": "stream_begin", "id": sid, "auto_tts": True})
                pcm = _make_pcm(1000)
                for i in range(3):
                    _send_seg(ws, sid, i, pcm)
                ws.send_json({"type": "stream_end", "id": sid})

                msgs, audio_data = _collect(ws)

                begins = [m for m in msgs if m["type"] == "audio_begin"]
                ends = [m for m in msgs if m["type"] == "audio_end"]
                transcripts = [m for m in msgs if m["type"] == "transcript"]

                assert len(begins) == 1
                assert begins[0]["id"] == sid
                assert begins[0]["parent_id"] is None
                assert len(ends) == 1
                assert ends[0]["id"] == sid
                assert len(transcripts) == 3
                assert [t["index"] for t in transcripts] == [0, 1, 2]
                assert len(audio_data) > 0


class TestFeatures:
    def test_speak_stream_feature(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                ready = _do_hello(ws)
                assert ready["features"]["speak_stream"] is True


class TestBusyExclusion:
    def test_second_stream_begin_is_busy(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = SlowFakeTTSClient()
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                ws.send_json({"type": "stream_begin", "id": "s1", "auto_tts": True})
                ws.send_json({"type": "stream_begin", "id": "s2", "auto_tts": True})
                err = ws.receive_json()
                assert err["type"] == "error"
                assert "busy" in err["message"]
                assert err["id"] == "s2"

    def test_speak_while_stream_is_busy(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = SlowFakeTTSClient()
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                ws.send_json({"type": "stream_begin", "id": "s1", "auto_tts": True})
                ws.send_json({"type": "speak", "id": "sp1", "text": "hello"})
                err = ws.receive_json()
                assert err["type"] == "error"
                assert "busy" in err["message"]
                assert err["id"] == "sp1"

    def test_stt_begin_while_stream_is_busy(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = SlowFakeTTSClient()
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                ws.send_json({"type": "stream_begin", "id": "s1", "auto_tts": True})
                ws.send_json({
                    "type": "stt_begin",
                    "id": "u1",
                    "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "auto_tts": False,
                })
                err = ws.receive_json()
                assert err["type"] == "error"
                assert "busy" in err["message"]
                assert err["id"] == "u1"


class TestCancel:
    def test_cancel_sends_stream_stopped(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = SlowFakeTTSClient()
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                sid = "stream1"
                ws.send_json({"type": "stream_begin", "id": sid, "auto_tts": True})
                pcm = _make_pcm(1000)
                _send_seg(ws, sid, 0, pcm)

                import time
                time.sleep(0.15)

                ws.send_json({"type": "cancel", "id": sid})

                msgs, _ = _collect(ws)
                stopped = [m for m in msgs if m["type"] == "stream_stopped"]
                legacy = [m for m in msgs if m["type"] == "stopped"]
                assert len(stopped) == 1
                assert stopped[0]["id"] == sid
                assert len(legacy) == 0

    def test_cancel_stt_suppresses_transcript(self) -> None:
        cfg = _make_config()
        block_event = threading.Event()
        stt = BlockingFakeSTTEngine(block_event=block_event)
        tts = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                sid = "stream1"
                ws.send_json({"type": "stream_begin", "id": sid, "auto_tts": True})
                pcm = _make_pcm(1000)
                _send_seg(ws, sid, 0, pcm)

                import time
                time.sleep(0.15)

                ws.send_json({"type": "cancel", "id": sid})
                block_event.set()

                msgs, _ = _collect(ws)
                transcripts = [m for m in msgs if m["type"] == "transcript"]
                assert len(transcripts) == 0
                stopped = [m for m in msgs if m["type"] == "stream_stopped"]
                assert len(stopped) == 1

    def test_idempotent_stop(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = SlowFakeTTSClient()
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                sid = "stream1"
                ws.send_json({"type": "stream_begin", "id": sid, "auto_tts": True})
                pcm = _make_pcm(1000)
                _send_seg(ws, sid, 0, pcm)

                import time
                time.sleep(0.15)

                ws.send_json({"type": "cancel", "id": sid})
                ws.send_json({"type": "cancel", "id": sid})

                msgs: list[dict] = []
                for _ in range(50):
                    received = ws.receive()
                    if "text" in received:
                        msg = json.loads(received["text"])
                        msgs.append(msg)
                        if msg["type"] in ("stream_stopped", "error"):
                            break

                stopped = [m for m in msgs if m["type"] == "stream_stopped"]
                assert len(stopped) == 1


class TestRTFSeam:
    def test_delayed_tts_preserves_order(self) -> None:
        cfg = _make_config()
        stt = IndexFakeSTTEngine()
        tts = FakeTTSClient(chunks=3, delay=0.02)
        with TestClient(create_app(cfg, tts_client=tts, stt_engine=stt)) as client:
            with client.websocket_connect("/ws") as ws:
                _do_hello(ws)
                sid = "stream1"
                ws.send_json({"type": "stream_begin", "id": sid, "auto_tts": True})
                pcm = _make_pcm(1000)
                for i in range(3):
                    _send_seg(ws, sid, i, pcm)
                ws.send_json({"type": "stream_end", "id": sid})

                msgs, audio_data = _collect(ws)

                begins = [m for m in msgs if m["type"] == "audio_begin"]
                ends = [m for m in msgs if m["type"] == "audio_end"]
                transcripts = [m for m in msgs if m["type"] == "transcript"]

                assert len(begins) == 1
                assert len(ends) == 1
                assert len(transcripts) == 3
                assert [t["index"] for t in transcripts] == [0, 1, 2]
                assert len(audio_data) > 0
