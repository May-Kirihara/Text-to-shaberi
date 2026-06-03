"""サーバエントリポイント（R0 stub: 設定ロード + bind検証 + uvicorn起動）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_server_config, validate_bind

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "server.toml"


def run_server(config_path: str | Path) -> None:
    import uvicorn

    cfg = load_server_config(config_path)
    validate_bind(cfg.bind)

    from .ws_server import create_app

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.bind.host, port=cfg.bind.port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(prog="text-to-shaberi-server")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="server.toml のパス")
    args = parser.parse_args()
    run_server(args.config)
