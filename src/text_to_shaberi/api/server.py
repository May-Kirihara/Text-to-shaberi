"""ローカルAPIサーバ (FastAPI + uvicorn)。

M0: 雛形（/speak, /control, /healthz）と、別スレッドでの起動/graceful shutdown。
WS /stream や割り込みの本実装は M3。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


class SpeakRequest(BaseModel):
    text: str
    profile: str | None = None
    priority: int = 0
    interrupt_policy: str | None = None


class ControlRequest(BaseModel):
    command: str  # stop | cancel | clear


def create_app(
    *,
    on_speak: Callable[[dict], None],
    on_control: Callable[[dict], None],
    auth_token: str = "",
) -> FastAPI:
    """発話/制御コールバックを束ねた FastAPI アプリを生成する。"""
    app = FastAPI(title="text-to-shaberi", version="0.1.0")

    def _authorize(token: str | None) -> None:
        # auth_token が空ならローカル運用としてノーチェック（既定 127.0.0.1）
        if auth_token and token != auth_token:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/speak")
    async def speak(req: SpeakRequest, authorization: str | None = Header(default=None)) -> dict:
        _authorize(authorization)
        on_speak(req.model_dump())
        return {"status": "queued"}

    @app.post("/control")
    async def control(req: ControlRequest, authorization: str | None = Header(default=None)) -> dict:
        _authorize(authorization)
        on_control(req.model_dump())
        return {"status": "ok"}

    return app


class APIServer:
    """uvicorn を別スレッドで起動/停止するラッパ（Qtアプリ内蔵用）。"""

    def __init__(self, app: FastAPI, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.run, name="api-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
