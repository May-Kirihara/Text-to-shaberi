"""歯車設定ダイアログ（全項目版）。

サーバ/マイク/出力 を QGroupBox で編集。
values() は update_client_toml 互換のドット section 型付き dict。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent,
        cfg,
        profiles=None,
        input_devices=None,
        output_devices=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")

        root_layout = QVBoxLayout(self)

        server_group = QGroupBox("サーバ")
        server_form = QFormLayout(server_group)

        self.address_edit = QLineEdit()
        self.address_edit.setText(getattr(cfg.server, "address", ""))
        server_form.addRow("アドレス:", self.address_edit)

        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setText(getattr(cfg.server, "token", ""))
        server_form.addRow("トークン:", self.token_edit)

        self.codec_combo = QComboBox()
        self.codec_combo.addItem("opus", "opus")
        self.codec_combo.addItem("pcm", "pcm")
        current_codec = getattr(cfg.server, "codec", "opus")
        codec_idx = self.codec_combo.findData(current_codec)
        if codec_idx >= 0:
            self.codec_combo.setCurrentIndex(codec_idx)
        server_form.addRow("コーデック:", self.codec_combo)

        self.tls_insecure_check = QCheckBox()
        self.tls_insecure_check.setChecked(getattr(cfg.server, "tls_insecure", False))
        server_form.addRow("TLS検証無効:", self.tls_insecure_check)

        self.tls_cafile_edit = QLineEdit()
        self.tls_cafile_edit.setText(getattr(cfg.server, "tls_cafile", ""))
        server_form.addRow("TLS CAファイル:", self.tls_cafile_edit)

        self.profile_combo = QComboBox()
        self.profile_combo.addItem("(サーバ既定)", "")
        active_id = getattr(cfg.profile, "active_id", "")
        selected_idx = 0
        for pi in profiles or []:
            self.profile_combo.addItem(pi.name if pi.name else pi.id, pi.id)
            if pi.id == active_id:
                selected_idx = self.profile_combo.count() - 1
        self.profile_combo.setCurrentIndex(selected_idx)
        server_form.addRow("プロファイル:", self.profile_combo)

        root_layout.addWidget(server_group)

        mic_group = QGroupBox("マイク")
        mic_form = QFormLayout(mic_group)

        self.mic_device_combo = QComboBox()
        self.mic_device_combo.addItem("既定", "")
        mic_cfg = getattr(cfg, "mic", None)
        mic_device = getattr(mic_cfg, "device", "default")
        for dev in input_devices or []:
            self.mic_device_combo.addItem(f"{dev.name} [{dev.index}]", dev.index)
        self._select_device_combo(self.mic_device_combo, mic_device)
        mic_form.addRow("デバイス:", self.mic_device_combo)

        vad_cfg = getattr(mic_cfg, "vad", None) if mic_cfg else None

        self.aggressiveness_spin = QSpinBox()
        self.aggressiveness_spin.setRange(0, 3)
        self.aggressiveness_spin.setValue(getattr(vad_cfg, "aggressiveness", 2) if vad_cfg else 2)
        mic_form.addRow("VAD感度(0-3):", self.aggressiveness_spin)

        self.min_silence_ms_spin = QSpinBox()
        self.min_silence_ms_spin.setRange(100, 3000)
        self.min_silence_ms_spin.setValue(getattr(vad_cfg, "min_silence_ms", 500) if vad_cfg else 500)
        mic_form.addRow("最小無音ms:", self.min_silence_ms_spin)

        self.speech_pad_ms_spin = QSpinBox()
        self.speech_pad_ms_spin.setRange(0, 1000)
        self.speech_pad_ms_spin.setValue(getattr(vad_cfg, "speech_pad_ms", 150) if vad_cfg else 150)
        mic_form.addRow("パディングms:", self.speech_pad_ms_spin)

        preprocess_cfg = getattr(mic_cfg, "preprocess", None) if mic_cfg else None

        self.gate_check = QCheckBox()
        self.gate_check.setChecked(getattr(preprocess_cfg, "gate", True) if preprocess_cfg else True)
        mic_form.addRow("ゲート:", self.gate_check)

        self.gate_threshold_spin = QSpinBox()
        self.gate_threshold_spin.setRange(0, 5000)
        self.gate_threshold_spin.setValue(
            getattr(preprocess_cfg, "gate_threshold", 200) if preprocess_cfg else 200
        )
        mic_form.addRow("ゲート閾値:", self.gate_threshold_spin)

        self.seg_silence_ms_spin = QSpinBox()
        self.seg_silence_ms_spin.setRange(100, 2000)
        self.seg_silence_ms_spin.setValue(getattr(vad_cfg, "seg_silence_ms", 250) if vad_cfg else 250)
        mic_form.addRow("サブseg無音ms:", self.seg_silence_ms_spin)

        self.end_silence_ms_spin = QSpinBox()
        self.end_silence_ms_spin.setRange(100, 3000)
        self.end_silence_ms_spin.setValue(getattr(vad_cfg, "end_silence_ms", 500) if vad_cfg else 500)
        mic_form.addRow("発話末無音ms:", self.end_silence_ms_spin)

        self.seg_min_ms_spin = QSpinBox()
        self.seg_min_ms_spin.setRange(50, 2000)
        self.seg_min_ms_spin.setValue(getattr(vad_cfg, "seg_min_ms", 400) if vad_cfg else 400)
        mic_form.addRow("最小seg ms:", self.seg_min_ms_spin)

        self.seg_max_ms_spin = QSpinBox()
        self.seg_max_ms_spin.setRange(500, 10000)
        self.seg_max_ms_spin.setValue(getattr(vad_cfg, "seg_max_ms", 2500) if vad_cfg else 2500)
        mic_form.addRow("最大seg ms:", self.seg_max_ms_spin)

        self.stream_mode_check = QCheckBox()
        self.stream_mode_check.setChecked(getattr(mic_cfg, "stream_mode", False) if mic_cfg else False)
        mic_form.addRow("既定でストリーミング:", self.stream_mode_check)

        root_layout.addWidget(mic_group)

        output_group = QGroupBox("出力")
        output_form = QFormLayout(output_group)

        self.output_device_combo = QComboBox()
        self.output_device_combo.addItem("既定", "default")
        self.output_device_combo.addItem("TTS_Virtual", "__ttsvirt__")
        for dev in output_devices or []:
            self.output_device_combo.addItem(f"{dev.name} [{dev.index}]", dev.index)
        output_device = getattr(cfg.output, "device", "default")
        self._select_output_combo(output_device)
        output_form.addRow("デバイス:", self.output_device_combo)

        root_layout.addWidget(output_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

    @staticmethod
    def _select_device_combo(combo, value) -> None:
        if value in (None, "", "default"):
            combo.setCurrentIndex(0)
            return
        try:
            int_val = int(value)
            idx = combo.findData(int_val)
            if idx >= 0:
                combo.setCurrentIndex(idx)
                return
        except (TypeError, ValueError):
            pass
        for i in range(combo.count()):
            text = combo.itemText(i)
            if text and value and value.lower() in text.lower():
                combo.setCurrentIndex(i)
                return

    def _select_output_combo(self, value) -> None:
        if value in (None, "", "default"):
            self.output_device_combo.setCurrentIndex(0)
            return
        if value == "__ttsvirt__":
            idx = self.output_device_combo.findData("__ttsvirt__")
            if idx >= 0:
                self.output_device_combo.setCurrentIndex(idx)
            return
        try:
            int_val = int(value)
            idx = self.output_device_combo.findData(int_val)
            if idx >= 0:
                self.output_device_combo.setCurrentIndex(idx)
                return
        except (TypeError, ValueError):
            pass
        for i in range(self.output_device_combo.count()):
            text = self.output_device_combo.itemText(i)
            if text and value and value.lower() in text.lower():
                self.output_device_combo.setCurrentIndex(i)
                return

    def values(self) -> dict:
        mic_dev_data = self.mic_device_combo.currentData()
        if mic_dev_data is None or mic_dev_data == "":
            mic_dev_str = ""
        elif isinstance(mic_dev_data, int):
            mic_dev_str = str(mic_dev_data)
        else:
            mic_dev_str = str(mic_dev_data)

        out_dev_data = self.output_device_combo.currentData()
        if out_dev_data is None:
            out_dev_str = "default"
        elif isinstance(out_dev_data, int):
            out_dev_str = str(out_dev_data)
        else:
            out_dev_str = str(out_dev_data)

        return {
            "server": {
                "address": self.address_edit.text(),
                "token": self.token_edit.text(),
                "codec": self.codec_combo.currentData() or "opus",
                "tls_insecure": self.tls_insecure_check.isChecked(),
                "tls_cafile": self.tls_cafile_edit.text(),
            },
            "profile": {
                "active_id": self.profile_combo.currentData() or "",
            },
            "mic": {
                "device": mic_dev_str,
                "stream_mode": self.stream_mode_check.isChecked(),
            },
            "mic.vad": {
                "aggressiveness": self.aggressiveness_spin.value(),
                "min_silence_ms": self.min_silence_ms_spin.value(),
                "speech_pad_ms": self.speech_pad_ms_spin.value(),
                "seg_silence_ms": self.seg_silence_ms_spin.value(),
                "end_silence_ms": self.end_silence_ms_spin.value(),
                "seg_min_ms": self.seg_min_ms_spin.value(),
                "seg_max_ms": self.seg_max_ms_spin.value(),
            },
            "mic.preprocess": {
                "gate": self.gate_check.isChecked(),
                "gate_threshold": self.gate_threshold_spin.value(),
            },
            "output": {
                "device": out_dev_str,
            },
        }
