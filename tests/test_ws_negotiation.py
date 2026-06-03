"""test_ws_negotiation: ネゴシエーション + token + TLS 配線テスト。

TestClient.websocket_connect を使用。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from text_to_shaberi.common.audio_format import AudioFormat
from text_to_shaberi.common.opus_codec import OPUS_CODEC_NAME, opus_available
from text_to_shaberi.server.config import (
    BindConfig,
    ServerConfig,
    STTConfig,
    TTSConfig,
    TransportConfig,
    normalize_transport_codec,
    validate_tls,
)
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


class _FakeSTTEngine:
    def transcribe(self, samples, sample_rate, language):
        return ""


class FakeTTSClient:
    def __init__(self, chunks: int = 3, chunk_samples: int = 160):
        self._chunks = chunks
        self._chunk_bytes = b"\x00\x00" * chunk_samples

    async def stream(self, profile, text, **kw) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            yield self._chunk_bytes

    async def aclose(self):
        pass


def _make_config(**overrides: Any) -> ServerConfig:
    bind_kw: dict[str, Any] = {}
    for k in ("host", "port", "token", "tls_cert", "tls_key"):
        if k in overrides:
            bind_kw[k] = overrides[k]
    transport_kw: dict[str, Any] = {}
    for k in ("codec", "opus_bitrate"):
        if k in overrides:
            transport_kw[k] = overrides[k]
    return ServerConfig(
        bind=BindConfig(**bind_kw) if bind_kw else BindConfig(),
        stt=STTConfig(backend="qwen_local", language="ja"),
        tts=TTSConfig(profiles_dir="", active_profile="gpt-sovits"),
        transport=TransportConfig(**transport_kw) if transport_kw else TransportConfig(),
    )


def _ws_connect(client: TestClient, **kwargs):
    return client.websocket_connect("/ws", **kwargs)


def _do_hello(ws, caps=None):
    hello = {"type": "hello", "protocol_version": 1}
    if caps is not None:
        hello["caps"] = caps
    ws.send_json(hello)
    return ws.receive_json()


class TestNegotiation:
    def test_opus_caps_with_opus_transport(self) -> None:
        cfg = _make_config(codec="opus")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws, caps={"codecs": [OPUS_CODEC_NAME, "pcm_s16le"]})
                assert ready["type"] == "ready"
                if opus_available():
                    assert ready["codec"] == OPUS_CODEC_NAME
                else:
                    assert ready["codec"] == "pcm_s16le"
                assert "opus" in ready["limits"]
                assert ready["limits"]["opus"]["rate"] == 48000
                assert ready["limits"]["opus"]["frame_ms"] == 20
                assert ready["limits"]["opus"]["bitrate"] == 24000

    def test_pcm_only_caps_selects_pcm(self) -> None:
        cfg = _make_config(codec="opus")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws, caps={"codecs": ["pcm_s16le"]})
                assert ready["type"] == "ready"
                assert ready["codec"] == "pcm_s16le"

    def test_pcm_transport_always_pcm(self) -> None:
        cfg = _make_config(codec="pcm")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws, caps={"codecs": [OPUS_CODEC_NAME, "pcm_s16le"]})
                assert ready["type"] == "ready"
                assert ready["codec"] == "pcm_s16le"

    def test_opus_fallback_warning_logged(self) -> None:
        cfg = _make_config(codec="opus")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client) as ws:
                with patch("text_to_shaberi.server.orchestrator.logger") as mock_logger:
                    ready = _do_hello(ws, caps={"codecs": ["pcm_s16le"]})
                    assert ready["codec"] == "pcm_s16le"
                    assert mock_logger.warning.called or not opus_available()

    def test_backward_compat_no_caps(self) -> None:
        cfg = _make_config()
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws)
                assert ready["type"] == "ready"
                assert ready["codec"] in ("pcm_s16le", OPUS_CODEC_NAME)

    def test_opus_limits_always_present(self) -> None:
        cfg = _make_config(codec="pcm")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws)
                assert "opus" in ready["limits"]
                assert "available" in ready["limits"]["opus"]


class TestToken:
    def test_correct_bearer_accepted(self) -> None:
        cfg = _make_config(token="secret123")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client, headers={"Authorization": "Bearer secret123"}) as ws:
                ready = _do_hello(ws)
                assert ready["type"] == "ready"

    def test_correct_query_accepted(self) -> None:
        cfg = _make_config(token="secret123")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client, params={"token": "secret123"}) as ws:
                ready = _do_hello(ws)
                assert ready["type"] == "ready"

    def test_wrong_token_rejected(self) -> None:
        cfg = _make_config(token="secret123")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with pytest.raises(Exception):
                with _ws_connect(client, headers={"Authorization": "Bearer wrong"}) as ws:
                    _do_hello(ws)

    def test_empty_token_skips_check(self) -> None:
        cfg = _make_config(token="")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(client) as ws:
                ready = _do_hello(ws)
                assert ready["type"] == "ready"

    def test_bearer_preferred_over_query(self) -> None:
        cfg = _make_config(token="secret123")
        fake = FakeTTSClient()
        with TestClient(create_app(cfg, tts_client=fake, stt_engine=_FakeSTTEngine())) as client:
            with _ws_connect(
                client,
                headers={"Authorization": "Bearer secret123"},
                params={"token": "wrong"},
            ) as ws:
                ready = _do_hello(ws)
                assert ready["type"] == "ready"


class TestValidateTLS:
    def test_both_empty_ok(self) -> None:
        validate_tls(BindConfig())

    def test_both_set_nonexistent_raises(self) -> None:
        with pytest.raises(ValueError, match="tls_cert"):
            validate_tls(BindConfig(tls_cert="/no/such/cert.pem", tls_key="/no/such/key.pem"))

    def test_cert_only_raises(self) -> None:
        with pytest.raises(ValueError, match="tls_key"):
            validate_tls(BindConfig(tls_cert="/some/cert.pem"))

    def test_key_only_raises(self) -> None:
        with pytest.raises(ValueError, match="tls_cert"):
            validate_tls(BindConfig(tls_key="/some/key.pem"))

    def test_both_set_existing_files(self, tmp_path) -> None:
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_bytes(b"cert")
        key.write_bytes(b"key")
        validate_tls(BindConfig(tls_cert=str(cert), tls_key=str(key)))


class TestNormalizeTransportCodec:
    def test_pcm(self) -> None:
        assert normalize_transport_codec("pcm") == "pcm_s16le"

    def test_pcm_s16le(self) -> None:
        assert normalize_transport_codec("pcm_s16le") == "pcm_s16le"

    def test_opus(self) -> None:
        assert normalize_transport_codec("opus") == OPUS_CODEC_NAME

    def test_unknown_fallback(self) -> None:
        assert normalize_transport_codec("unknown") == "pcm_s16le"


class TestTLSInApp:
    def test_run_server_passes_ssl_args(self, tmp_path) -> None:
        import uvicorn as real_uvicorn

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_bytes(b"cert")
        key.write_bytes(b"key")

        toml_path = tmp_path / "server.toml"
        toml_path.write_text(
            '[bind]\nhost="127.0.0.1"\nport=0\ntoken=""\ntls_cert="'
            + str(cert)
            + '"\ntls_key="'
            + str(key)
            + '"\n[transport]\ncodec="pcm"\n'
        )

        with patch.object(real_uvicorn, "run") as mock_run:
            from text_to_shaberi.server.app import run_server

            run_server(str(toml_path))
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs[1].get("ssl_certfile") == str(cert)
            assert call_kwargs[1].get("ssl_keyfile") == str(key)

    def test_run_server_no_ssl(self, tmp_path) -> None:
        import uvicorn as real_uvicorn

        toml_path = tmp_path / "server.toml"
        toml_path.write_text(
            '[bind]\nhost="127.0.0.1"\nport=0\ntoken=""\n[transport]\ncodec="pcm"\n'
        )

        with patch.object(real_uvicorn, "run") as mock_run:
            from text_to_shaberi.server.app import run_server

            run_server(str(toml_path))
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert "ssl_certfile" not in call_kwargs[1]
            assert "ssl_keyfile" not in call_kwargs[1]
