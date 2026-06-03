"""メインウィンドウ（M2: テキスト入力・送信・停止・出力デバイス切替・ログ表示）。

R0: MainWindow は ClientConfig のみで生成（旧 config.active_tts_profile/config.stt.backend は参照しない）。
初期ログは server.address と profile.active_id を表示。
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class MainWindow(QMainWindow):
    def __init__(self, config, output_devices=None, on_speak: Callable[[str], None] | None = None):
        super().__init__()
        self.config = config
        self._on_speak = on_speak
        self._on_stop: Callable[[], None] | None = None

        self.setWindowTitle("Text-to-shaberi")
        self.resize(680, 460)

        central = QWidget()
        layout = QVBoxLayout(central)

        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("出力先:"))
        self.device_combo = QComboBox()
        self.device_combo.addItem("既定デバイス（システム既定sink）", None)
        self.device_combo.addItem("TTS_Virtual（配信用）", "__ttsvirt__")
        for dev in output_devices or []:
            self.device_combo.addItem(f"{dev.name}", dev.index)
        device_row.addWidget(self.device_combo, 1)
        layout.addLayout(device_row)

        input_row = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("読み上げるテキストを入力…")
        self.send_button = QPushButton("送信")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        input_row.addWidget(self.text_input, 1)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.stop_button)
        layout.addLayout(input_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

        self.setCentralWidget(central)

        self.send_button.clicked.connect(self._handle_send)
        self.text_input.returnPressed.connect(self._handle_send)
        self.stop_button.clicked.connect(self._handle_stop)

        server_addr = getattr(config, "server", None)
        addr_str = getattr(server_addr, "address", "N/A") if server_addr else "N/A"
        profile_cfg = getattr(config, "profile", None)
        active_id = getattr(profile_cfg, "active_id", "") if profile_cfg else ""
        self.log(f"server = {addr_str} / profile = {active_id or '(server default)'}")

    def set_on_speak(self, callback: Callable[[str], None]) -> None:
        self._on_speak = callback

    def set_on_stop(self, callback: Callable[[], None]) -> None:
        self._on_stop = callback

    def selected_output_device(self) -> int | None | str:
        return self.device_combo.currentData()

    def set_busy(self, busy: bool) -> None:
        """送信中UIの有効/無効を切替（同時発話防止）。"""
        self.text_input.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def _handle_send(self) -> None:
        text = self.text_input.text().strip()
        if not text:
            return
        self.text_input.clear()
        self.log(f"[gui] {text}")
        if self._on_speak is not None:
            self._on_speak(text)

    def _handle_stop(self) -> None:
        if self._on_stop is not None:
            self._on_stop()

    def log(self, message: str) -> None:
        self.log_view.appendPlainText(message)
