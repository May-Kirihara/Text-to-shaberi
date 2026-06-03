"""エントリポイント（M2: ストリーミング再生＋音声I/O）。

- `text-to-shaberi`            … GUI 起動
- `text-to-shaberi --check`    … GUIを起動せず設定/プロファイル/デバイスのロードを検査
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .config import AppConfig, load_config
from .tts.profile import load_profile

# リポジトリ直下の config.toml（src/text_to_shaberi/app.py から2つ上）
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.toml"


def _load_output_devices() -> list:
    """sounddevice 列挙。libportaudio2 未導入等で失敗したら空リストで縮退。"""
    try:
        from .audio.devices import list_output_devices

        return list_output_devices()
    except Exception as exc:  # noqa: BLE001 - 起動を止めない
        print(f"[warn] 音声デバイス列挙に失敗: {exc}", file=sys.stderr)
        return []


def run_check(config_path: str | Path) -> None:
    cfg: AppConfig = load_config(config_path)
    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)

    print("== config ==")
    print("  active_tts_profile:", cfg.active_tts_profile)
    print("  stt               :", cfg.stt.backend, "| lang:", cfg.stt.language)
    print("  mic               :", f"{cfg.mic.sample_rate}Hz {cfg.mic.channels}ch vad={cfg.mic.vad.engine}")
    print("  output.device     :", cfg.output.device)
    print("  api               :", f"{cfg.api.host}:{cfg.api.port} enabled={cfg.api.enabled}")
    print("== tts profile ==")
    print("  name  :", profile.name, "| driver:", profile.driver)
    print("  url   :", profile.method, profile.url)
    print("  audio :", profile.audio)
    print("  body  :", profile.render_body("こんにちは、テストです。"))
    print("== output devices ==")
    devices = _load_output_devices()
    for dev in devices:
        print(f"  [{dev.index}] {dev.name} ({int(dev.default_samplerate)}Hz, out={dev.max_output_channels})")
    if not devices:
        print("  (なし / 列挙不可)")


def _setup_input_method(cfg: AppConfig) -> None:
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
        return  # system: 継承のまま

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
    import qasync
    from PySide6.QtWidgets import QApplication

    from .audio.player import StreamingPlayer, play as play_audio
    from .audio.routing import move_app_to_sink
    from .tts.client import TTSClient
    from .ui.main_window import MainWindow

    cfg = load_config(config_path)
    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)

    # PulseAudio クライアント名固定（QApplication 生成前・sounddevice import 前）
    os.environ.setdefault(
        "PULSE_PROP",
        f"application.name=text-to-shaberi media.role=production",
    )

    _setup_input_method(cfg)

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    client = TTSClient()
    window = MainWindow(cfg, output_devices=_load_output_devices())

    current_task: list[asyncio.Task | None] = [None]
    current_player: list[StreamingPlayer | None] = [None]

    async def speak(text: str) -> None:
        device = window.selected_output_device()
        use_virtual = device == "__ttsvirt__"
        play_device = None if use_virtual else device
        window.set_busy(True)
        player: StreamingPlayer | None = None
        try:
            current_task[0] = asyncio.current_task()
            if profile.response_mode == "audio_stream":
                player = await loop.run_in_executor(
                    None,
                    lambda: StreamingPlayer(
                        profile.audio.sample_rate,
                        profile.audio.channels,
                        play_device,
                        profile.audio.encoding,
                    ),
                )
                current_player[0] = player
                if use_virtual:
                    moved = await loop.run_in_executor(
                        None,
                        lambda: move_app_to_sink("ttsvirt", "text-to-shaberi", os.getpid()),
                    )
                    window.log(f"[route] TTS_Virtual move: {moved}")
                t0 = loop.time()
                first_chunk: float | None = None
                async for chunk in client.stream(profile, text):
                    if first_chunk is None:
                        first_chunk = loop.time() - t0
                        window.log(f"[tts] first-chunk {first_chunk * 1000:.0f}ms")
                    await loop.run_in_executor(None, player.write_bytes, chunk)
                await loop.run_in_executor(None, player.close)
                window.log(f"[tts] 再生完了 {loop.time() - t0:.2f}s")
            else:
                t0 = loop.time()
                buf = await client.synthesize(profile, text)
                window.log(f"[tts] {buf.duration:.1f}s 音声を {loop.time() - t0:.2f}s で取得 → 再生")
                await loop.run_in_executor(None, play_audio, buf.samples, buf.sample_rate, play_device)
        except asyncio.CancelledError:
            if player is not None:
                await loop.run_in_executor(None, player.abort)
            window.log("[tts] 停止しました")
        except Exception as exc:
            if player is not None:
                await loop.run_in_executor(None, player.abort)
            window.log(f"[error] {exc!r}")
        finally:
            current_task[0] = None
            current_player[0] = None
            window.set_busy(False)

    def on_speak(text: str) -> None:
        if current_task[0] is not None:
            return
        asyncio.ensure_future(speak(text))

    def on_stop() -> None:
        if current_player[0] is not None:
            loop.run_in_executor(None, current_player[0].abort)
        if current_task[0] is not None:
            current_task[0].cancel()

    window.set_on_speak(on_speak)
    window.set_on_stop(on_stop)

    def on_about_to_quit() -> None:
        if current_task[0] is not None:
            current_task[0].cancel()
        if current_player[0] is not None:
            current_player[0].abort()
        asyncio.ensure_future(client.aclose())

    app.aboutToQuit.connect(on_about_to_quit)
    window.show()

    with loop:
        loop.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(prog="text-to-shaberi")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="config.toml のパス")
    parser.add_argument(
        "--check",
        action="store_true",
        help="GUIを起動せず設定/プロファイル/デバイスのロードを検査して終了",
    )
    args = parser.parse_args()

    if args.check:
        run_check(args.config)
    else:
        run_gui(args.config)
