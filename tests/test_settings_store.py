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

        class BadStr:
            def __str__(self):
                return "ok"

        with pytest.raises(Exception):
            update_client_toml(p, {"server": {"address": BadStr()}})

        assert p.read_text(encoding="utf-8") == original
