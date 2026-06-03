"""サーバエントリポイント（R0 stub: 設定ロード + bind検証 + uvicorn起動）。"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_server_config, validate_bind, validate_tls

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "server.toml"

logger = logging.getLogger(__name__)


def run_server(config_path: str | Path) -> None:
    import uvicorn

    from ..common.opus_codec import opus_available

    cfg = load_server_config(config_path)
    validate_bind(cfg.bind)
    validate_tls(cfg.bind)

    avail = opus_available()
    print(f"[opus] available: {avail}")

    if cfg.bind.tls_cert and cfg.bind.tls_key:
        print(f"[tls] cert={cfg.bind.tls_cert} key={cfg.bind.tls_key}")
    else:
        import ipaddress

        try:
            addr = ipaddress.ip_address(cfg.bind.host)
            if not addr.is_loopback:
                logger.warning(
                    "非loopback bind ('%s') かつ TLS 未設定。"
                    " token は平文で流れる。wss/TLS 推奨。",
                    cfg.bind.host,
                )
        except ValueError:
            if cfg.bind.host not in ("localhost", "::1"):
                logger.warning(
                    "bind.host '%s' は非loopbackの可能性があり TLS 未設定。",
                    cfg.bind.host,
                )

    from .ws_server import create_app

    app = create_app(cfg)

    ssl_kwargs: dict = {}
    if cfg.bind.tls_cert and cfg.bind.tls_key:
        ssl_kwargs["ssl_certfile"] = cfg.bind.tls_cert
        ssl_kwargs["ssl_keyfile"] = cfg.bind.tls_key

    uvicorn.run(
        app,
        host=cfg.bind.host,
        port=cfg.bind.port,
        log_level="info",
        **ssl_kwargs,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="text-to-shaberi-server")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="server.toml のパス")
    args = parser.parse_args()
    run_server(args.config)
