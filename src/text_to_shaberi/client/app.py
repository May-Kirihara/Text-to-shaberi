"""クライアントエントリポイント（run_gui＋IME＋--check 移設）。

- `text-to-shaberi-client`        … GUI 起動
- `text-to-shaberi-client --check` … GUIを起動せず設定/デバイスのロードを検査
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import load_client_config

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "client.toml"


def _load_output_devices() -> list:
    """sounddevice 列挙。libportaudio2 未導入等で失敗したら空リストで縮退。"""
    try:
        from .audio.devices import list_output_devices

        return list_output_devices()
    except Exception as exc:  # noqa: BLE001 - 起動を止めない
        print(f"[warn] 音声デバイス列挙に失敗: {exc}", file=sys.stderr)
        return []


def run_check(config_path: str | Path) -> None:
    cfg = load_client_config(config_path)

    print("== client config ==")
    print("  server.address :", cfg.server.address)
    print("  server.token   :", "(set)" if cfg.server.token else "(empty)")
    print("  output.device  :", cfg.output.device)
    print("  mic            :", f"{cfg.mic.sample_rate}Hz {cfg.mic.channels}ch vad={cfg.mic.vad.engine}")
    print("  ui.input_method:", cfg.ui.input_method)
    print("  profile.active_id:", cfg.profile.active_id or "(server default)")
    print("== output devices ==")
    devices = _load_output_devices()
    for dev in devices:
        print(f"  [{dev.index}] {dev.name} ({int(dev.default_samplerate)}Hz, out={dev.max_output_channels})")
    if not devices:
        print("  (なし / 列挙不可)")


def _setup_input_method(cfg) -> None:
    """QApplication 生成前に日本語入力(IME)を準備する。

    X11+PySide6(Qt6.11)ではシステムの fcitx5 Qt6プラグイン(Qt6.4)が版差で読めないため、
    既定では同梱の ibus プラグイン＋ibus-mozc を使う（アプリだけ ibus、デスクトップは fcitx5 のまま）。
    """
    method = getattr(cfg.ui, "input_method", "ibus")

    if method == "virtualkeyboard":
        os.environ["QT_IM_MODULE"] = "qtvirtualkeyboard"
        print("[ime] Qt仮想キーボードを使用")
        return
    if method != "ibus":
        return

    import shutil
    import subprocess
    import time

    ibus_daemon = shutil.which("ibus-daemon")
    if ibus_daemon is None:
        print("[ime] ibus-daemon が見つかりません → 入力方式はシステム既定のまま", file=sys.stderr)
        return
    try:
        running = subprocess.run(["pgrep", "-x", "ibus-daemon"], capture_output=True).returncode == 0
        if not running:
            subprocess.Popen([ibus_daemon, "-dr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(12):
                time.sleep(0.15)
                if subprocess.run(["pgrep", "-x", "ibus-daemon"], capture_output=True).returncode == 0:
                    break
            print("[ime] ibus-daemon を起動しました")

        os.environ["QT_IM_MODULE"] = "ibus"
        os.environ["XMODIFIERS"] = "@im=ibus"

        ibus_cli = shutil.which("ibus")
        if ibus_cli:
            for engine in ("mozc-jp", "com.google.IBus.Mozc"):
                if subprocess.run([ibus_cli, "engine", engine], capture_output=True).returncode == 0:
                    print(f"[ime] ibusエンジン: {engine}")
                    break
        print("[ime] 日本語入力=ibus+mozc（トグルキーはmozc/ibus設定に従う）")
    except Exception as exc:  # noqa: BLE001 - IME準備の失敗で起動を止めない
        print(f"[ime] ibusセットアップ失敗（英数のみで継続）: {exc}", file=sys.stderr)


def run_gui(config_path: str | Path) -> None:
    import asyncio

    import qasync
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    cfg = load_client_config(config_path)

    os.environ.setdefault(
        "PULSE_PROP",
        f"application.name=text-to-shaberi media.role=production",
    )

    _setup_input_method(cfg)

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow(cfg, output_devices=_load_output_devices())

    def on_speak(text: str) -> None:
        window.log("[client] WS未接続（C1で実装）")

    def on_stop() -> None:
        window.log("[client] WS未接続（C1で実装）")

    window.set_on_speak(on_speak)
    window.set_on_stop(on_stop)

    window.show()

    with loop:
        loop.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(prog="text-to-shaberi-client")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="client.toml のパス")
    parser.add_argument(
        "--check",
        action="store_true",
        help="GUIを起動せず設定/デバイスのロードを検査して終了",
    )
    args = parser.parse_args()

    if args.check:
        run_check(args.config)
    else:
        run_gui(args.config)
