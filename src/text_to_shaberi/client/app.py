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


def _load_input_devices() -> list:
    try:
        from .audio.devices import list_input_devices

        return list_input_devices()
    except Exception as exc:
        print(f"[warn] 入力デバイス列挙に失敗: {exc}", file=sys.stderr)
        return []


def run_check(config_path: str | Path) -> None:
    from ..common.opus_codec import opus_available

    cfg = load_client_config(config_path)
    avail = opus_available()

    print("== client config ==")
    print("  server.address :", cfg.server.address)
    print("  server.token   :", "(set)" if cfg.server.token else "(empty)")
    print("  server.codec   :", cfg.server.codec)
    print("  opus available :", avail)
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

    import qasync
    from PySide6.QtWidgets import QApplication, QTabWidget

    from ..common.protocol import AudioFormatPayload
    from .ui.main_window import MainWindow
    from .ui.srt_tab import SrtTabWidget
    from .srt_player import SrtPlayer
    from .ws_client import WSClient
    from .stream_bridge import StreamBridge
    from .text_stream_bridge import TextStreamBridge
    from .audio_monitor import AudioMonitorServer
    from .caption_server import CaptionServer
    from .text_input import LocalInputServer, TextSegmenter
    from .audio.vad import StreamStart
    from .settings_store import update_client_toml

    cfg = load_client_config(config_path)

    os.environ.setdefault(
        "PULSE_PROP",
        "application.name=text-to-shaberi media.role=production",
    )

    _setup_input_method(cfg)

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow(cfg, output_devices=_load_output_devices())
    window.set_output_device(cfg.output.device)
    window.set_mic_enabled(False)  # ready まで mic 無効（codex MED①）

    central_widget = window.takeCentralWidget()
    tab_widget = QTabWidget()
    tab_widget.addTab(central_widget, "メイン")
    srt_tab = SrtTabWidget()
    tab_widget.addTab(srt_tab, "台本")
    window.setCentralWidget(tab_widget)

    _profiles: list = []
    mic_enabled_by_user = [False]
    shutting_down = [False]
    mic_reconfiguring = [False]

    _local_server_holder: list = [None]
    _monitor_server_holder: list = [None]
    _caption_server_holder: list = [None]

    def _on_audio(kind: str, payload) -> None:
        # capture stream の復号音声を、テキスト投入元のローカル接続へ返す（§10）。
        srv = _local_server_holder[0]
        if srv is not None:
            srv.send_audio(kind, payload)

    def _on_monitor(kind: str, payload) -> None:
        # 全合成音声を音声モニタの全購読者へ broadcast（口パク等の外部エンジン向け）。
        srv = _monitor_server_holder[0]
        if srv is not None:
            srv.broadcast(kind, payload)

    def _on_caption(event: str, payload) -> None:
        # 「今喋ってる内容」を配信字幕オーバーレイの全購読者へ broadcast。
        srv = _caption_server_holder[0]
        if srv is not None:
            srv.broadcast(event, payload)

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
        codec_pref=cfg.server.codec,
        tls_insecure=cfg.server.tls_insecure,
        tls_cafile=cfg.server.tls_cafile,
        token_in_query=cfg.server.token_in_query,
        auto_reconnect=cfg.server.reconnect,
        reconnect_max_delay=cfg.server.reconnect_max_delay,
        prime_ms=cfg.output.prime_ms,
        on_audio=_on_audio,
        on_monitor=_on_monitor,
        on_caption=_on_caption,
        caption_max=cfg.caption.max_chars,
        caption_hold_base_ms=cfg.caption.hold_base_ms,
        caption_hold_char_ms=cfg.caption.hold_char_ms,
        caption_hold_max_ms=cfg.caption.hold_max_ms,
    )

    mic_holder: list = [None]
    mic_stream_mode = [cfg.mic.stream_mode]
    active_mic_mode = [None]
    stream_supported = [False]
    stream_bridge = StreamBridge(ws, on_log=window.log, loop=loop)

    srt_player = SrtPlayer(loop, ws)
    _srt_paused = [False]
    _srt_entries_holder = [None]

    def _srt_load_file():
        from PySide6.QtWidgets import QFileDialog
        from .srt_parser import load_srt_file

        path, _ = QFileDialog.getOpenFileName(
            window, "SRTファイルを選択", "", "SRT Files (*.srt);;All Files (*)"
        )
        if path:
            entries = load_srt_file(path)
            if entries:
                _srt_entries_holder[0] = entries
                srt_tab.set_entries(entries)
                window.log(f"[srt] {len(entries)}件読み込み: {path}")
            else:
                window.log(f"[srt] 読み込み失敗: {path}")
                window.set_status(f"SRT読み込み失敗: {path}")

    async def _srt_play():
        entries = _srt_entries_holder[0]
        if not entries:
            return
        _srt_paused[0] = False
        window.set_busy(True)
        srt_tab.set_playing(True)
        srt_tab.progress_bar.setValue(0)

        def _on_entry(idx, total, entry):
            srt_tab.set_current_entry(idx, total)

        wait_pb = srt_tab.wait_playback_check.isChecked()
        await srt_player.play(entries, profile=cfg.profile.active_id or None, on_entry=_on_entry, wait_for_playback=wait_pb)
        srt_tab.set_playing(False)
        srt_tab.progress_bar.setValue(0)
        window.set_busy(False)

    def _srt_pause_toggle():
        _srt_paused[0] = not _srt_paused[0]
        if _srt_paused[0]:
            srt_player.pause()
            srt_tab.set_paused(True)
        else:
            srt_player.resume()
            srt_tab.set_paused(False)

    async def _srt_stop():
        await srt_player.stop()
        srt_tab.set_playing(False)
        srt_tab.set_paused(False)
        window.set_busy(False)

    srt_tab.play_button.clicked.connect(lambda: asyncio.ensure_future(_srt_play()))
    srt_tab.pause_button.clicked.connect(lambda: _srt_pause_toggle())
    srt_tab.stop_button.clicked.connect(lambda: asyncio.ensure_future(_srt_stop()))
    srt_tab.load_file_button.clicked.connect(lambda: _srt_load_file())

    text_supported = [False]
    text_segmenter = None
    text_stream_bridge = None
    local_input_server = None
    monitor_server = None
    caption_server = None

    if cfg.input.enabled:
        text_segmenter = TextSegmenter(
            max_chars=cfg.input.seg_max_chars,
            hard_max_chars=cfg.input.seg_hard_max_chars,
        )

        def _text_on_done() -> None:
            srv = _local_server_holder[0]
            if srv is not None:
                asyncio.ensure_future(srv.notify({"event": "done"}))

        def _text_on_error(message: str) -> None:
            srv = _local_server_holder[0]
            if srv is not None:
                asyncio.ensure_future(
                    srv.notify({"event": "error", "message": message})
                )

        text_stream_bridge = TextStreamBridge(
            ws,
            on_log=window.log,
            on_done=_text_on_done,
            on_error=_text_on_error,
            loop=loop,
            capture=cfg.input.forward_audio,
        )

        def _text_on_text(text: str, profile):
            if active_mic_mode[0] is not None:
                return "busy: mic active"
            for ev in text_segmenter.feed(text):
                if isinstance(ev, StreamStart):
                    # 実際に stream が始まる時だけ予約（空 text では呼ばない）。
                    text_stream_bridge.set_profile(profile)
                    local_input_server.mark_active()
                text_stream_bridge.handle_event(ev)
            return None

        def _text_on_final():
            for ev in text_segmenter.flush():
                text_stream_bridge.handle_event(ev)
            return None

        def _text_on_cancel() -> None:
            # bridge.abort() が pending 破棄＋in-flight stream の ws.cancel＋idle後 on_done を担う。
            text_segmenter.reset()
            text_stream_bridge.abort()

        local_input_server = LocalInputServer(
            host=cfg.input.host,
            port=cfg.input.port,
            token=cfg.input.token,
            on_text=_text_on_text,
            on_final=_text_on_final,
            on_cancel=_text_on_cancel,
            on_log=window.log,
            loop=loop,
        )
        _local_server_holder[0] = local_input_server

    if cfg.monitor.enabled:
        monitor_server = AudioMonitorServer(
            host=cfg.monitor.host,
            port=cfg.monitor.port,
            token=cfg.monitor.token,
            queue_max=cfg.monitor.queue_max,
            on_log=window.log,
            loop=loop,
        )
        _monitor_server_holder[0] = monitor_server

    if cfg.caption.enabled:
        caption_server = CaptionServer(
            host=cfg.caption.host,
            port=cfg.caption.port,
            token=cfg.caption.token,
            template=cfg.caption.template,
            delay_ms=cfg.caption.delay_ms,
            on_log=window.log,
            loop=loop,
        )
        _caption_server_holder[0] = caption_server

    def _on_state(event: str, payload: object = None) -> None:
        if event == "ready":
            codec_str = getattr(payload, "codec", None) or "?"
            window.set_connection(f"\u2713 codec:{codec_str}")
            nonlocal _profiles
            _profiles = payload.profiles if payload else []
            feats = payload.features if payload else {}
            has_auto_tts = feats.get("auto_tts")
            stream_supported[0] = bool(feats.get("speak_stream"))
            stream_bridge.set_supported(stream_supported[0])
            window.set_stream_mode_available(stream_supported[0])
            text_supported[0] = bool(feats.get("text_stream"))
            if text_stream_bridge is not None:
                text_stream_bridge.set_supported(text_supported[0])
                if not text_supported[0]:
                    window.log("[input] サーバが text_stream 未対応 → one-shot speak にfallback")
            if not has_auto_tts:
                window.log("[mic] サーバが auto_tts 非対応: 認識のみ")
                window.set_mic_enabled(False)
            else:
                window.set_mic_enabled(True)
            if mic_stream_mode[0] and not stream_supported[0]:
                mic_stream_mode[0] = False
                window.set_stream_mode(False)
                window.log("[stream] サーバが speak_stream 未対応 → 発話単位に切替")
                if active_mic_mode[0] == "stream" and mic_enabled_by_user[0]:
                    asyncio.ensure_future(_reconfigure_mic_async())
        elif event == "busy":
            window.set_busy(True)
            stream_bridge.notify_ws_busy()
            if text_stream_bridge is not None:
                text_stream_bridge.notify_ws_busy()
            if active_mic_mode[0] == "utterance" and mic_holder[0] is not None and mic_holder[0].active and not shutting_down[0] and not mic_reconfiguring[0]:
                mic_holder[0].pause()
        elif event == "idle":
            if not srt_player.is_playing:
                window.set_busy(False)
                stream_bridge.notify_ws_idle()
                if text_stream_bridge is not None:
                    text_stream_bridge.notify_ws_idle()
                if active_mic_mode[0] == "utterance" and mic_enabled_by_user[0] and mic_holder[0] is not None and mic_holder[0].active and not shutting_down[0] and not mic_reconfiguring[0]:
                    mic_holder[0].resume()
            else:
                stream_bridge.notify_ws_idle()
                if text_stream_bridge is not None:
                    text_stream_bridge.notify_ws_idle()
        elif event == "connected":
            window.log("[ws] connected")
        elif event == "disconnected":
            window.set_connection("\u2717")
            window.set_busy(False)
        elif event == "error":
            window.set_status(f"\u26a0 {payload}")
            window.log(f"[error] {payload}")
        elif event == "reconnecting":
            window.set_connection(f"\u518d\u63a5\u7d9a\u4e2d\u2026({payload})")
        elif event == "transcript":
            window.log(f"\U0001f3a4\u2192 {payload}")
        elif event == "metrics":
            p = payload or {}
            codec_val = p.get("codec") or "?"
            parts = [f"codec:{codec_val}"]
            stt = p.get("stt_ms")
            if stt is not None:
                parts.append(f"STT {stt/1000:.2f}s")
            ttfa = p.get("ttfa_ms")
            if ttfa is not None:
                parts.append(f"TTFA {ttfa:.0f}ms")
            window.set_status(" | ".join(parts))

    ws._on_state = _on_state

    def on_speak(text: str) -> None:
        asyncio.ensure_future(ws.speak(text))

    def on_stop() -> None:
        asyncio.ensure_future(ws.cancel())

    def on_settings() -> None:
        from .ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(
            window, cfg, _profiles,
            input_devices=_load_input_devices(),
            output_devices=_load_output_devices(),
        )
        if dlg.exec():
            vals = dlg.values()
            update_client_toml(config_path, vals)

            server_vals = vals.get("server", {})
            cfg.server.address = server_vals.get("address", cfg.server.address)
            cfg.server.token = server_vals.get("token", cfg.server.token)
            cfg.server.codec = server_vals.get("codec", cfg.server.codec)
            cfg.server.tls_insecure = server_vals.get("tls_insecure", cfg.server.tls_insecure)
            cfg.server.tls_cafile = server_vals.get("tls_cafile", cfg.server.tls_cafile)

            profile_vals = vals.get("profile", {})
            cfg.profile.active_id = profile_vals.get("active_id", cfg.profile.active_id)
            ws._default_profile = cfg.profile.active_id

            mic_vals = vals.get("mic", {})
            cfg.mic.device = mic_vals.get("device", cfg.mic.device)

            vad_vals = vals.get("mic.vad", {})
            if vad_vals:
                cfg.mic.vad.aggressiveness = vad_vals.get("aggressiveness", cfg.mic.vad.aggressiveness)
                cfg.mic.vad.min_silence_ms = vad_vals.get("min_silence_ms", cfg.mic.vad.min_silence_ms)
                cfg.mic.vad.speech_pad_ms = vad_vals.get("speech_pad_ms", cfg.mic.vad.speech_pad_ms)
                cfg.mic.vad.seg_silence_ms = vad_vals.get("seg_silence_ms", cfg.mic.vad.seg_silence_ms)
                cfg.mic.vad.end_silence_ms = vad_vals.get("end_silence_ms", cfg.mic.vad.end_silence_ms)
                cfg.mic.vad.seg_min_ms = vad_vals.get("seg_min_ms", cfg.mic.vad.seg_min_ms)
                cfg.mic.vad.seg_max_ms = vad_vals.get("seg_max_ms", cfg.mic.vad.seg_max_ms)

            mic_only_vals = vals.get("mic", {})
            if "stream_mode" in mic_only_vals:
                cfg.mic.stream_mode = bool(mic_only_vals["stream_mode"])
                mic_stream_mode[0] = cfg.mic.stream_mode and stream_supported[0]
                window.set_stream_mode(mic_stream_mode[0])

            preprocess_vals = vals.get("mic.preprocess", {})
            if preprocess_vals:
                cfg.mic.preprocess.gate = preprocess_vals.get("gate", cfg.mic.preprocess.gate)
                cfg.mic.preprocess.gate_threshold = preprocess_vals.get(
                    "gate_threshold", cfg.mic.preprocess.gate_threshold
                )

            output_vals = vals.get("output", {})
            cfg.output.device = output_vals.get("device", cfg.output.device)

            caption_vals = vals.get("caption", {})
            if "delay_ms" in caption_vals:
                cfg.caption.delay_ms = int(caption_vals["delay_ms"])
                srv = _caption_server_holder[0]
                if srv is not None:
                    srv.set_delay(cfg.caption.delay_ms)  # 起動中OBSへ即時反映

            ws._apply_server_settings(
                codec_pref=cfg.server.codec,
                tls_insecure=cfg.server.tls_insecure,
                tls_cafile=cfg.server.tls_cafile,
                token=cfg.server.token,
            )
            asyncio.ensure_future(ws.reconnect(cfg.server.address, cfg.server.token))

            window.set_output_device(cfg.output.device)
            if mic_enabled_by_user[0]:
                asyncio.ensure_future(_reconfigure_mic_async())

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
        mic_enabled_by_user[0] = active
        if active:
            desired = "stream" if (mic_stream_mode[0] and stream_supported[0]) else "utterance"
            if mic_holder[0] is None:
                from .audio.mic import MicCapture
                from .audio.devices import resolve_device

                dev = resolve_device(cfg.mic.device, kind="input")
                if desired == "stream":
                    stream_bridge.start()
                    mic_holder[0] = MicCapture(
                        sample_rate=cfg.mic.sample_rate,
                        channels=cfg.mic.channels,
                        device=dev,
                        frame_ms=cfg.mic.vad.frame_ms,
                        vad_config=cfg.mic,
                        on_event=stream_bridge.handle_event,
                        on_log=window.log,
                        loop=loop,
                        stream_mode=True,
                    )
                else:
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
                active_mic_mode[0] = desired
            try:
                mic_holder[0].start()
                window.log(f"[mic] 録音開始 ({active_mic_mode[0]})")
            except Exception as exc:
                window.log(f"[mic] 開始失敗: {exc}")
                window.set_mic_active(False)
        else:
            if mic_holder[0] is not None:
                try:
                    mic_holder[0].stop()
                except Exception:
                    pass
                mic_holder[0] = None
                window.log("[mic] 録音停止")
            if active_mic_mode[0] == "stream":
                asyncio.ensure_future(stream_bridge.close())
            active_mic_mode[0] = None

    async def _reconfigure_mic_async() -> None:
        mic_reconfiguring[0] = True
        try:
            if mic_holder[0] is not None:
                await asyncio.to_thread(mic_holder[0].stop)
                mic_holder[0] = None
            if active_mic_mode[0] == "stream":
                await stream_bridge.close()
            active_mic_mode[0] = None
        finally:
            mic_reconfiguring[0] = False
        if mic_enabled_by_user[0]:
            on_mic_toggle(True)

    def on_mode_toggle(stream: bool) -> None:
        mic_stream_mode[0] = stream
        cfg.mic.stream_mode = stream
        try:
            update_client_toml(config_path, {"mic": {"stream_mode": stream}})
        except Exception as exc:
            window.log(f"[settings] モード保存失敗: {exc}")
        window.log(f"[mic] モード: {'ストリーミング' if stream else '発話単位'}")
        if mic_enabled_by_user[0]:
            asyncio.ensure_future(_reconfigure_mic_async())

    def on_mute_toggle(muted: bool) -> None:
        ws.set_muted(muted)
        window.log(f"[gui] スピーカー{'ミュート' if muted else 'ミュート解除'}")

    window.set_on_speak(on_speak)
    window.set_on_stop(on_stop)
    window.set_on_settings(on_settings)
    window.set_on_mic_toggle(on_mic_toggle)
    window.set_on_mode_toggle(on_mode_toggle)
    window.set_on_mute_toggle(on_mute_toggle)
    ws.set_muted(cfg.output.muted)
    window.set_muted(cfg.output.muted)
    window.set_stream_mode(mic_stream_mode[0])
    window.set_stream_mode_available(False)

    window.show()

    async def _startup():
        await ws.connect()
        if local_input_server is not None:
            text_stream_bridge.start()
            try:
                await local_input_server.start()
            except Exception as exc:
                window.log(f"[input] 入力口の起動失敗: {exc}")
        if monitor_server is not None:
            try:
                await monitor_server.start()
            except Exception as exc:
                window.log(f"[monitor] 音声モニタの起動失敗: {exc}")
        if caption_server is not None:
            try:
                await caption_server.start()
            except Exception as exc:
                window.log(f"[caption] 字幕サーバの起動失敗: {exc}")

    asyncio.ensure_future(_startup())

    async def _shutdown():
        shutting_down[0] = True
        if mic_holder[0] is not None:
            await asyncio.to_thread(mic_holder[0].stop)
        await stream_bridge.close()
        if local_input_server is not None:
            await local_input_server.close()
        if text_stream_bridge is not None:
            await text_stream_bridge.close()
        # ws を broadcast サーバより先に閉じる: ws teardown 中の _finish_stream が出す
        # 最後の caption clear / monitor end を、購読サーバが開いているうちに配信させる
        # （逆順だと閉鎖済みサーバへ送られ OBS に古字幕が残る・codex 実装レビュー#1）。
        await ws.close()
        if monitor_server is not None:
            await monitor_server.close()
        if caption_server is not None:
            await caption_server.close()

    def _on_quit() -> None:
        # 終了時: loop が回っている間だけ shutdown を schedule（停止後の ensure_future で落とさない）
        try:
            if loop.is_running():
                asyncio.ensure_future(_shutdown())
        except Exception:
            pass

    app.aboutToQuit.connect(_on_quit)

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
