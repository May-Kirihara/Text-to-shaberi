"""WS サーバ FastAPI アプリ（R0 stub: /healthz + /ws stub）。

S1 で本実装。create_app() は TestClient で叩ける形に。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """FastAPI アプリを生成する。TestClient で直接叩ける。"""
    app = FastAPI(title="text-to-shaberi-server", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await ws.send_json({"type": "ready", "session_id": "stub"})
            logger.info("[ws] 接続受理（R0 stub・S1で本実装）")
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            logger.info("[ws] クライアント切断")

    return app
