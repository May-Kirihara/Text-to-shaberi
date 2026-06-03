"""クライアントエントリポイント（run_gui＋IME＋--check 移設）。

- `text-to-shaberi-client`        … GUI 起動
- `text-to-shaberi-client --check` … GUIを起動せず設定/デバイスのロードを検査
C1: WSClient/PlaybackSink 配線・on_speak/on_stop/on_settings・startup接続・再接続。
C2: MicCapture 配線・on_mic_toggle/on_segment・ハーフデュプレックス・stt_format。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .config import load_client_config

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "client.toml"


def _load_output_devices() -> list:
    try:
        from .audio.devices import list_output_devices

        return list_output_devices()
    except Exception as exc:
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
    except Exception as exc:
        print(f"[ime] ibusセットアップ失敗（英数のみで継続）: {exc}", file=sys.stderr)


def run_gui(config_path: str | Path) -> None:
    import asyncio

    import qasync
    from PySide6.QtWidgets import QApplication

    from ..common.protocol import AudioFormatPayload
    from .ui.main_window import MainWindow
    from .ws_client import WSClient
    from .settings_store import update_client_toml

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

    _profiles: list = []

    ws = WSClient(
        cfg.server.address,
        cfg.server.token,
        on_log=window.log,
        on_state=None,
        device_provider=window.selected_output_device,
        loop=loop,
        default_profile=cfg.profile.active_id,
        stt_format=AudioFormatPayload(
            codec="pcm_s16le",
            sample_rate=cfg.mic.sample_rate,
            channels=cfg.mic.channels,
        ),
    )

    mic_holder: list = [None]

    def _on_state(event: str, payload: object = None) -> None:
        if event == "ready":
            window.set_connection("\u2713")
            nonlocal _profiles
            _profiles = payload.profiles if payload else []
            has_auto_tts = payload.features.get("auto_tts") if payload else False
            if not has_auto_tts:
                window.log("[mic] サーバが auto_tts 非対応: 認識のみ")
                window.set_mic_enabled(False)
            else:
                window.set_mic_enabled(True)
        elif event == "busy":
            window.set_busy(True)
        elif event == "idle":
            window.set_busy(False)
        elif event == "connected":
            window.log("[ws] connected")
        elif event == "disconnected":
            window.set_connection("\u2717")
            window.set_busy(False)
        elif event == "error":
            window.log(f"[error] {payload}")

    ws._on_state = _on_state

    def on_speak(text: str) -> None:
        asyncio.ensure_future(ws.speak(text))

    def on_stop() -> None:
        asyncio.ensure_future(ws.cancel())

    def on_settings() -> None:
        from .ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(window, cfg, _profiles)
        if dlg.exec():
            vals = dlg.values()
            update_client_toml(
                config_path,
                {
                    "server": {"address": vals["address"], "token": vals["token"]},
                    "profile": {"active_id": vals["active_id"]},
                },
            )
            cfg.server.address = vals["address"]
            cfg.server.token = vals["token"]
            cfg.profile.active_id = vals["active_id"]
            ws._default_profile = vals["active_id"]
            asyncio.ensure_future(ws.reconnect(vals["address"], vals["token"]))

    _last_skip_log = [0.0]

    def on_segment(seg: bytes) -> None:
        if ws.is_busy():
            now = loop.time()
            if now - _last_skip_log[0] >= 1.0:
                window.log("[mic] 発話中につきスキップ")
                _last_skip_log[0] = now
            return
        asyncio.ensure_future(ws.utter(seg, auto_tts=True))

    def on_mic_toggle(active: bool) -> None:
        if active:
            if mic_holder[0] is None:
                from .audio.mic import MicCapture
                from .audio.devices import resolve_device

                dev = resolve_device(cfg.mic.device, kind="input")
                mic_holder[0] = MicCapture(
                    sample_rate=cfg.mic.sample_rate,
                    channels=cfg.mic.channels,
                    device=dev,
                    frame_ms=cfg.mic.vad.frame_ms,
                    vad_config=cfg.mic,
                    on_segment=on_segment,
                    on_log=window.log,
                    loop=loop,
                )
            try:
                mic_holder[0].start()
                window.log("[mic] 録音開始")
            except Exception as exc:
                window.log(f"[mic] 開始失敗: {exc}")
                window.set_mic_active(False)
        else:
            if mic_holder[0] is not None:
                mic_holder[0].stop()
                window.log("[mic] 録音停止")

    window.set_on_speak(on_speak)
    window.set_on_stop(on_stop)
    window.set_on_settings(on_settings)
    window.set_on_mic_toggle(on_mic_toggle)

    window.show()

    async def _startup():
        await ws.connect()

    asyncio.ensure_future(_startup())

    async def _shutdown():
        if mic_holder[0] is not None:
            await asyncio.to_thread(mic_holder[0].stop)
        await ws.close()

    app.aboutToQuit.connect(lambda: asyncio.ensure_future(_shutdown()))

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
