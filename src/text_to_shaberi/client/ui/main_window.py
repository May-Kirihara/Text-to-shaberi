"""メインウィンドウ（M2: テキスト入力・送信・停止・出力デバイス切替・ログ表示）。

R0: MainWindow は ClientConfig のみで生成（旧 config.active_tts_profile/config.stt.backend は参照しない）。
初期ログは server.address と profile.active_id を表示。
C1: 歯車ボタン＋接続状態ラベルを追加。
C2: マイクトグル「🎤」＋set_on_mic_toggle/set_mic_active/set_mic_enabled。
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

_MIC_LABEL_ON = "🎤 ON"
_MIC_LABEL_OFF = "🎤"
_MIC_ACCESSIBLE_NAME = "マイク"
_MIC_TOOLTIP = "マイク常時録音 ON/OFF"


class MainWindow(QMainWindow):
    def __init__(self, config, output_devices=None, on_speak: Callable[[str], None] | None = None):
        super().__init__()
        self.config = config
        self._on_speak = on_speak
        self._on_stop: Callable[[], None] | None = None
        self._on_settings: Callable[[], None] | None = None
        self._on_mic_toggle: Callable[[bool], None] | None = None
        self._on_mode_toggle: Callable[[bool], None] | None = None

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

        self.settings_button = QPushButton("\u2699")
        self.settings_button.setFixedWidth(32)
        self.settings_button.clicked.connect(self._handle_settings)
        device_row.addWidget(self.settings_button)

        layout.addLayout(device_row)

        self.connection_label = QLabel("接続: --")
        layout.addWidget(self.connection_label)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        input_row = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("読み上げるテキストを入力…")
        self.send_button = QPushButton("送信")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)

        self.mic_button = QPushButton(_MIC_LABEL_OFF)
        self.mic_button.setCheckable(True)
        self.mic_button.setAccessibleName(_MIC_ACCESSIBLE_NAME)
        self.mic_button.setToolTip(_MIC_TOOLTIP)
        self.mic_button.setFixedWidth(56)
        self.mic_button.toggled.connect(self._handle_mic_toggle)

        self.mode_button = QPushButton("発話単位")
        self.mode_button.setCheckable(True)
        self.mode_button.setFixedWidth(96)
        self.mode_button.setToolTip("発話単位 / ストリーミング(喋りながら順次TTS) 切替")
        self.mode_button.toggled.connect(self._handle_mode_toggle)
        self.mode_button.setEnabled(False)

        input_row.addWidget(self.mode_button)
        input_row.addWidget(self.mic_button)
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

    def set_on_settings(self, callback: Callable[[], None]) -> None:
        self._on_settings = callback

    def set_on_mic_toggle(self, callback: Callable[[bool], None]) -> None:
        self._on_mic_toggle = callback

    def set_mic_active(self, active: bool) -> None:
        self.mic_button.setChecked(active)
        if active:
            self.mic_button.setText(_MIC_LABEL_ON)
        else:
            self.mic_button.setText(_MIC_LABEL_OFF)

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_button.setEnabled(enabled)

    def selected_output_device(self) -> int | None | str:
        return self.device_combo.currentData()

    def set_busy(self, busy: bool) -> None:
        self.text_input.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def set_connection(self, text: str) -> None:
        self.connection_label.setText(f"接続: {text}")

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def set_output_device(self, value) -> None:
        if value == "default" or value == "" or value is None:
            self.device_combo.setCurrentIndex(0)
            return
        if value == "__ttsvirt__":
            idx = self.device_combo.findData("__ttsvirt__")
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)
            return
        try:
            int_val = int(value)
            idx = self.device_combo.findData(int_val)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)
                return
        except (TypeError, ValueError):
            pass
        if not isinstance(value, str):
            return  # int index で findData 不一致＝該当デバイス無し（名前マッチ不要）
        for i in range(self.device_combo.count()):
            data = self.device_combo.itemData(i)
            if isinstance(data, str) and data == value:
                self.device_combo.setCurrentIndex(i)
                return
            dev_name = self.device_combo.itemText(i)
            if dev_name and value and value.lower() in dev_name.lower():
                self.device_combo.setCurrentIndex(i)
                return

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

    def _handle_settings(self) -> None:
        if self._on_settings is not None:
            self._on_settings()

    def _handle_mic_toggle(self, checked: bool) -> None:
        if checked:
            self.mic_button.setText(_MIC_LABEL_ON)
        else:
            self.mic_button.setText(_MIC_LABEL_OFF)
        if self._on_mic_toggle is not None:
            self._on_mic_toggle(checked)

    def set_on_mode_toggle(self, callback: Callable[[bool], None]) -> None:
        self._on_mode_toggle = callback

    def _handle_mode_toggle(self, checked: bool) -> None:
        self.mode_button.setText("ストリーミング" if checked else "発話単位")
        if self._on_mode_toggle is not None:
            self._on_mode_toggle(checked)

    def set_stream_mode(self, stream: bool) -> None:
        self.mode_button.blockSignals(True)
        self.mode_button.setChecked(stream)
        self.mode_button.setText("ストリーミング" if stream else "発話単位")
        self.mode_button.blockSignals(False)

    def set_stream_mode_available(self, available: bool) -> None:
        self.mode_button.setEnabled(available)
        if not available:
            self.mode_button.setToolTip("サーバが speak_stream 未対応")
        else:
            self.mode_button.setToolTip("発話単位 / ストリーミング(喋りながら順次TTS) 切替")

    def log(self, message: str) -> None:
        self.log_view.appendPlainText(message)
