"""WS サーバ FastAPI アプリ（S2: /healthz + /ws + lifespan + STT）。

create_app(config, tts_client, stt_engine) で TestClient から注入可能。
lifespan で TTSClient / STTEngine の所有権を制御する。
"""
from __future__ import annotations

import concurrent.futures
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..common.protocol import ProfileInfo
from .config import ServerConfig, load_server_config
from .orchestrator import Session, SessionState
from .tts.profile import load_profile

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "server.toml"


def create_app(
    config: ServerConfig | None = None,
    tts_client: Any = None,
    stt_engine: Any = None,
) -> FastAPI:
    if config is None:
        config = load_server_config(_DEFAULT_CONFIG)

    profiles_dir = config.tts.profiles_dir or str(
        Path(__file__).resolve().parents[3] / "profiles"
    )
    available_profiles = _enumerate_profiles(profiles_dir)
    _owns_client = tts_client is None
    _injected_client = tts_client
    _owns_stt = stt_engine is None
    _injected_stt = stt_engine
    stt_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if _owns_client:
            from .tts.client import TTSClient

            app.state.tts_client = TTSClient()
        if _owns_stt:
            cfg: ServerConfig = app.state.config
            if cfg.stt.backend == "qwen_local":
                from .stt.engine import QwenSTTEngine

                app.state.stt_engine = QwenSTTEngine(cfg.stt.qwen_local)
            else:
                raise RuntimeError(f"unsupported STT backend: {cfg.stt.backend}")
        yield
        if _owns_client and app.state.tts_client is not None:
            await app.state.tts_client.aclose()
        app.state.stt_executor.shutdown(wait=False)

    app = FastAPI(title="text-to-shaberi-server", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.profiles_dir = profiles_dir
    app.state.available_profiles = available_profiles
    app.state.tts_client = _injected_client
    app.state.stt_engine = _injected_stt
    app.state.stt_executor = stt_executor

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        cfg: ServerConfig = app.state.config
        token = cfg.bind.token
        if token:
            query_token = ws.query_params.get("token", "")
            if not query_token:
                auth = ws.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    query_token = auth[7:]
            if query_token != token:
                await ws.close(code=1008)
                return

        await ws.accept()
        state = SessionState(
            config=cfg,
            profiles_dir=app.state.profiles_dir,
            tts_client=app.state.tts_client,
            available_profiles=app.state.available_profiles,
            stt_engine=app.state.stt_engine,
            stt_executor=app.state.stt_executor,
            language=cfg.stt.language,
        )
        session = Session(ws, state)
        try:
            await session.run()
        except WebSocketDisconnect:
            logger.info("[ws] クライアント切断: %s", session.session_id)

    return app


def _enumerate_profiles(profiles_dir: str) -> list[ProfileInfo]:
    tts_dir = Path(profiles_dir) / "tts"
    if not tts_dir.is_dir():
        return []
    result: list[ProfileInfo] = []
    for path in sorted(tts_dir.glob("*.toml")):
        try:
            prof = load_profile(profiles_dir, path.stem)
            result.append(ProfileInfo(id=path.stem, name=prof.name))
        except Exception:
            logger.warning("プロファイル読み込み失敗: %s", path, exc_info=True)
    return result
