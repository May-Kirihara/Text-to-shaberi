"""test_main_window: MainWindow の set_status/set_output_device 検証。

QT_QPA_PLATFORM=offscreen。widget 初期値・メソッド検証のみ。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from text_to_shaberi.client.config import ClientConfig
from text_to_shaberi.client.ui.main_window import MainWindow


class TestMainWindowStatus:
    def test_set_status_updates_label(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_status("codec:opus | STT 1.20s | TTFA 130ms")
        assert window._status_label.text() == "codec:opus | STT 1.20s | TTFA 130ms"

    def test_set_status_empty(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_status("")
        assert window._status_label.text() == ""


class TestMainWindowOutputDevice:
    def test_set_output_device_default(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_output_device("default")
        assert window.device_combo.currentIndex() == 0

    def test_set_output_device_none(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_output_device(None)
        assert window.device_combo.currentIndex() == 0

    def test_set_output_device_empty(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_output_device("")
        assert window.device_combo.currentIndex() == 0

    def test_set_output_device_ttsvirt(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_output_device("__ttsvirt__")
        idx = window.device_combo.currentIndex()
        assert window.device_combo.itemData(idx) == "__ttsvirt__"

    @dataclass
    class FakeDevice:
        name: str
        index: int

    def test_set_output_device_by_index(self, qapp):
        cfg = ClientConfig()
        devs = [
            self.FakeDevice(name="Speaker", index=5),
            self.FakeDevice(name="HDMI", index=7),
        ]
        window = MainWindow(cfg, output_devices=devs)
        window.set_output_device(5)
        assert window.device_combo.currentData() == 5

    def test_set_output_device_by_name(self, qapp):
        cfg = ClientConfig()
        devs = [
            self.FakeDevice(name="Speaker", index=5),
        ]
        window = MainWindow(cfg, output_devices=devs)
        window.set_output_device("Speaker")
        assert window.device_combo.currentData() == 5

    def test_selected_output_device_default(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        assert window.selected_output_device() is None

    def test_selected_output_device_ttsvirt(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_output_device("__ttsvirt__")
        assert window.selected_output_device() == "__ttsvirt__"


class TestMainWindowBusy:
    def test_set_busy_true(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_busy(True)
        assert not window.text_input.isEnabled()
        assert not window.send_button.isEnabled()
        assert window.stop_button.isEnabled()

    def test_set_busy_false(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_busy(True)
        window.set_busy(False)
        assert window.text_input.isEnabled()
        assert window.send_button.isEnabled()
        assert not window.stop_button.isEnabled()


class TestMainWindowConnection:
    def test_set_connection(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_connection("\u2713 codec:opus")
        assert "\u2713 codec:opus" in window.connection_label.text()


class TestMainWindowModeToggle:
    def test_on_mode_toggle_callback(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        calls = []
        window.set_on_mode_toggle(lambda c: calls.append(c))
        window.mode_button.setEnabled(True)
        window.mode_button.setChecked(True)
        assert calls == [True]

    def test_set_stream_mode_no_signal(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        calls = []
        window.set_on_mode_toggle(lambda c: calls.append(c))
        window.set_stream_mode(True)
        assert window.mode_button.isChecked() is True
        assert window.mode_button.text() == "ストリーミング"
        assert calls == []

    def test_set_stream_mode_available_false(self, qapp):
        cfg = ClientConfig()
        window = MainWindow(cfg)
        window.set_stream_mode_available(False)
        assert window.mode_button.isEnabled() is False
