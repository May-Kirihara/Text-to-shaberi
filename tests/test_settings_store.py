"""test_settings_store: update_client_toml のコメント保持・section 追記・エスケープ検証。"""
from __future__ import annotations

import os
import tempfile
import tomllib

import pytest

from text_to_shaberi.client.settings_store import update_client_toml


class TestSettingsStore:
    def test_existing_key_replace_preserves_comments(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "# サーバ設定\n"
            "[server]\n"
            "address = \"ws://old:8765/ws\"  # comment\n"
            "token = \"old\"\n"
            "\n"
            "# プロファイル\n"
            "[profile]\n"
            "active_id = \"prof1\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"server": {"address": "ws://new:9999/ws"}})

        text = p.read_text(encoding="utf-8")
        assert "# サーバ設定" in text
        assert "# プロファイル" in text
        data = tomllib.loads(text)
        assert data["server"]["address"] == "ws://new:9999/ws"
        assert data["server"]["token"] == "old"
        assert data["profile"]["active_id"] == "prof1"

    def test_missing_section_appended(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\naddress = \"ws://x\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"profile": {"active_id": "p2"}})

        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["profile"]["active_id"] == "p2"
        assert data["server"]["address"] == "ws://x"

    def test_missing_key_appended(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\naddress = \"ws://x\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"server": {"token": "abc"}})

        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["server"]["token"] == "abc"

    def test_dotted_section_not_misrecognized(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\n"
            "address = \"ws://x\"\n"
            "\n"
            "[server.auth]\n"
            "method = \"bearer\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"server": {"address": "ws://y"}})

        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["server"]["address"] == "ws://y"
        assert data["server"]["auth"]["method"] == "bearer"

    def test_array_table_not_misrecognized(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[[server]]\n"
            "name = \"s1\"\n"
            "\n"
            "[profile]\n"
            "active_id = \"p1\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"profile": {"active_id": "p2"}})

        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["server"][0]["name"] == "s1"
        assert data["profile"]["active_id"] == "p2"

    def test_comment_not_misrecognized_as_key(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\n"
            "# address = \"commented out\"\n"
            "address = \"ws://x\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"server": {"address": "ws://y"}})

        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["server"]["address"] == "ws://y"
        assert "# address" in text

    def test_value_with_newline_and_control_chars(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\naddress = \"ws://x\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"server": {"token": "line1\nline2\ttab\r\n"}})

        data = tomllib.loads(p.read_text(encoding="utf-8"))
        assert data["server"]["token"] == "line1\nline2\ttab\r\n"

    def test_value_with_quotes_and_backslash(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\naddress = \"ws://x\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"server": {"token": 'he said "hello" \\ end'}})

        data = tomllib.loads(p.read_text(encoding="utf-8"))
        assert data["server"]["token"] == 'he said "hello" \\ end'

    def test_atomic_write_no_corruption_on_bad_toml(self, tmp_path):
        p = tmp_path / "client.toml"
        original = "[server]\naddress = \"ws://x\"\n"
        p.write_text(original, encoding="utf-8")

        class BadObj:
            def __str__(self):
                raise RuntimeError("bad")

        with pytest.raises(Exception):
            update_client_toml(p, {"server": {"address": BadObj()}})

        assert p.read_text(encoding="utf-8") == original

    def test_bool_value_written_as_true_false(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text("[server]\naddress = \"ws://x\"\n", encoding="utf-8")
        update_client_toml(p, {"server": {"tls_insecure": True}})
        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["server"]["tls_insecure"] is True
        assert "true" in text

    def test_bool_false_value(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text("[server]\naddress = \"ws://x\"\n", encoding="utf-8")
        update_client_toml(p, {"server": {"tls_insecure": False}})
        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["server"]["tls_insecure"] is False
        assert "false" in text

    def test_int_value_written_as_number(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text("[mic.vad]\naggressiveness = 2\n", encoding="utf-8")
        update_client_toml(p, {"mic.vad": {"aggressiveness": 3}})
        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["mic"]["vad"]["aggressiveness"] == 3
        assert "aggressiveness = 3" in text

    def test_float_value_written_as_number(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text("[server]\naddress = \"ws://x\"\n", encoding="utf-8")
        update_client_toml(p, {"server": {"reconnect_max_delay": 60.5}})
        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["server"]["reconnect_max_delay"] == pytest.approx(60.5)

    def test_dotted_section_mic_vad_key_replace(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\naddress = \"ws://x\"\n\n"
            "[mic.vad]\naggressiveness = 2\nmin_silence_ms = 500\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"mic.vad": {"aggressiveness": 1, "min_silence_ms": 300}})
        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["mic"]["vad"]["aggressiveness"] == 1
        assert data["mic"]["vad"]["min_silence_ms"] == 300

    def test_dotted_section_mic_preprocess_new_keys(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text("[server]\naddress = \"ws://x\"\n", encoding="utf-8")
        update_client_toml(p, {"mic.preprocess": {"gate": False, "gate_threshold": 100}})
        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["mic"]["preprocess"]["gate"] is False
        assert data["mic"]["preprocess"]["gate_threshold"] == 100

    def test_operator_precedence_fix_no_false_positive(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text(
            "[server]\naddress = \"ws://x\"\n\n"
            "\n"
            "[profile]\nactive_id = \"p1\"\n",
            encoding="utf-8",
        )
        update_client_toml(p, {"profile": {"active_id": "p2"}})
        text = p.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        assert data["profile"]["active_id"] == "p2"
        assert data["server"]["address"] == "ws://x"

    def test_bool_before_int_not_1_or_0(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text("[mic.preprocess]\ngate = true\n", encoding="utf-8")
        update_client_toml(p, {"mic.preprocess": {"gate": False}})
        text = p.read_text(encoding="utf-8")
        assert "false" in text
        data = tomllib.loads(text)
        assert data["mic"]["preprocess"]["gate"] is False

    def test_str_value_still_quoted(self, tmp_path):
        p = tmp_path / "client.toml"
        p.write_text("[server]\naddress = \"ws://x\"\n", encoding="utf-8")
        update_client_toml(p, {"server": {"token": "my_secret"}})
        text = p.read_text(encoding="utf-8")
        assert '"my_secret"' in text
        data = tomllib.loads(text)
        assert data["server"]["token"] == "my_secret"
