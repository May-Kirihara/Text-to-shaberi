"""test_settings_dialog: SettingsDialog 全項目の初期値・values() 検証。

QT_QPA_PLATFORM=offscreen。widget 初期値/values 検証のみ。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from text_to_shaberi.client.config import (
    ClientConfig,
    MicConfig,
    MicPreprocessConfig,
    OutputConfig,
    ProfileConfig,
    ServerConfig,
    VADConfig,
)
from text_to_shaberi.client.ui.settings_dialog import SettingsDialog


@dataclass
class FakeProfile:
    id: str
    name: str


class TestSettingsDialogInit:
    def test_widgets_initialized_from_cfg(self, qapp):
        cfg = ClientConfig(
            server=ServerConfig(
                address="ws://example.com:8765/ws",
                token="mytoken",
                codec="opus",
                tls_insecure=True,
                tls_cafile="/path/to/ca",
            ),
            mic=MicConfig(
                device="default",
                vad=VADConfig(aggressiveness=3, min_silence_ms=300, speech_pad_ms=100),
                preprocess=MicPreprocessConfig(gate=False, gate_threshold=500),
            ),
            output=OutputConfig(device="default"),
            profile=ProfileConfig(active_id="prof1"),
        )
        profiles = [FakeProfile(id="prof1", name="Profile 1"), FakeProfile(id="prof2", name="Profile 2")]

        dlg = SettingsDialog(None, cfg, profiles=profiles)

        assert dlg.address_edit.text() == "ws://example.com:8765/ws"
        assert dlg.token_edit.text() == "mytoken"
        assert dlg.codec_combo.currentData() == "opus"
        assert dlg.tls_insecure_check.isChecked() is True
        assert dlg.tls_cafile_edit.text() == "/path/to/ca"
        assert dlg.profile_combo.currentData() == "prof1"
        assert dlg.aggressiveness_spin.value() == 3
        assert dlg.min_silence_ms_spin.value() == 300
        assert dlg.speech_pad_ms_spin.value() == 100
        assert dlg.gate_check.isChecked() is False
        assert dlg.gate_threshold_spin.value() == 500

    def test_values_returns_dot_section_dict(self, qapp):
        cfg = ClientConfig()
        dlg = SettingsDialog(None, cfg)
        vals = dlg.values()

        assert "server" in vals
        assert "profile" in vals
        assert "mic" in vals
        assert "mic.vad" in vals
        assert "mic.preprocess" in vals
        assert "output" in vals

    def test_values_server_types(self, qapp):
        cfg = ClientConfig()
        dlg = SettingsDialog(None, cfg)
        vals = dlg.values()

        server = vals["server"]
        assert isinstance(server["address"], str)
        assert isinstance(server["token"], str)
        assert isinstance(server["codec"], str)
        assert isinstance(server["tls_insecure"], bool)
        assert isinstance(server["tls_cafile"], str)

    def test_values_mic_vad_types(self, qapp):
        cfg = ClientConfig()
        dlg = SettingsDialog(None, cfg)
        vals = dlg.values()

        vad = vals["mic.vad"]
        assert isinstance(vad["aggressiveness"], int)
        assert isinstance(vad["min_silence_ms"], int)
        assert isinstance(vad["speech_pad_ms"], int)
        assert "seg_silence_ms" in vad
        assert "end_silence_ms" in vad
        assert "seg_min_ms" in vad
        assert "seg_max_ms" in vad

    def test_values_mic_stream_mode(self, qapp):
        cfg = ClientConfig()
        dlg = SettingsDialog(None, cfg)
        vals = dlg.values()
        assert "stream_mode" in vals["mic"]
        assert isinstance(vals["mic"]["stream_mode"], bool)

    def test_values_mic_preprocess_types(self, qapp):
        cfg = ClientConfig()
        dlg = SettingsDialog(None, cfg)
        vals = vals = dlg.values()

        preprocess = vals["mic.preprocess"]
        assert isinstance(preprocess["gate"], bool)
        assert isinstance(preprocess["gate_threshold"], int)

    def test_values_output_device_default(self, qapp):
        cfg = ClientConfig(output=OutputConfig(device="default"))
        dlg = SettingsDialog(None, cfg)
        vals = dlg.values()
        assert vals["output"]["device"] == "default"

    def test_values_output_device_ttsvirt(self, qapp):
        cfg = ClientConfig(output=OutputConfig(device="__ttsvirt__"))
        dlg = SettingsDialog(None, cfg)
        vals = dlg.values()
        assert vals["output"]["device"] == "__ttsvirt__"

    def test_device_enum_failure_empty_list(self, qapp):
        cfg = ClientConfig()
        dlg = SettingsDialog(None, cfg, input_devices=None, output_devices=None)
        vals = dlg.values()
        assert vals["mic"]["device"] == ""
        assert vals["output"]["device"] == "default"

    def test_profile_selection(self, qapp):
        cfg = ClientConfig(profile=ProfileConfig(active_id="prof2"))
        profiles = [FakeProfile(id="prof1", name="P1"), FakeProfile(id="prof2", name="P2")]
        dlg = SettingsDialog(None, cfg, profiles=profiles)
        assert dlg.profile_combo.currentData() == "prof2"
        vals = dlg.values()
        assert vals["profile"]["active_id"] == "prof2"

    def test_default_profile_when_none(self, qapp):
        cfg = ClientConfig(profile=ProfileConfig(active_id=""))
        dlg = SettingsDialog(None, cfg, profiles=None)
        assert dlg.profile_combo.currentData() == ""
        vals = dlg.values()
        assert vals["profile"]["active_id"] == ""
