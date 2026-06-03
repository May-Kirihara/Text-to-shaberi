"""import 境界テスト（AST 静的検査）。

client/** は server/** / torch / qwen_asr / silero_vad を import 禁止。
server/** は PySide6 / sounddevice を import 禁止。
common/** は torch / qwen_asr / silero_vad / PySide6 / sounddevice を import 禁止。
opuslib / soxr はトップレベル import 禁止（遅延/factory 内のみ）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "text_to_shaberi"

_CLIENT_FORBIDDEN = {
    "text_to_shaberi.server",
    "torch",
    "qwen_asr",
    "silero_vad",
}

_SERVER_FORBIDDEN = {
    "PySide6",
    "sounddevice",
}

_COMMON_FORBIDDEN = {
    "torch",
    "qwen_asr",
    "silero_vad",
    "PySide6",
    "sounddevice",
}

_LAZY_IMPORTS = {"opuslib", "soxr"}


def _gather_imports(directory: Path) -> list[tuple[Path, set[str], set[str]]]:
    """directory 配下の全 .py の (path, first_components, full_module_names) を返す。"""
    results: list[tuple[Path, set[str], set[str]]] = []
    for pyfile in sorted(directory.rglob("*.py")):
        first_parts: set[str] = set()
        full_names: set[str] = set()
        try:
            tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    first_parts.add(alias.name.split(".")[0])
                    full_names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    first_parts.add(node.module.split(".")[0])
                    full_names.add(node.module)
        results.append((pyfile, first_parts, full_names))
    return results


def _is_top_level_import(tree: ast.AST, module_first_part: str) -> bool:
    """module_first_part がトップレベル（関数/メソッド/class外）で import されているか。"""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == module_first_part:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == module_first_part:
                return True
    return False


class TestClientImportBoundaries:
    def test_client_does_not_import_server_or_torch(self) -> None:
        client_dir = _SRC / "client"
        if not client_dir.exists():
            pytest.skip("client/ ディレクトリ不在")
        for pyfile, first_parts, full_names in _gather_imports(client_dir):
            for forbidden in _CLIENT_FORBIDDEN:
                if forbidden == "text_to_shaberi.server":
                    for m in full_names:
                        assert not m.startswith("text_to_shaberi.server"), (
                            f"{pyfile.relative_to(_SRC)} imports server: {m}"
                        )
                else:
                    assert forbidden not in first_parts, (
                        f"{pyfile.relative_to(_SRC)} imports {forbidden}"
                    )

    def test_client_app_no_torch_in_sys_modules(self) -> None:
        """補助: import text_to_shaberi.client.config 後 sys.modules に torch/qwen_asr が無いこと。"""
        import importlib

        importlib.import_module("text_to_shaberi.client.config")
        assert "torch" not in sys.modules
        assert "qwen_asr" not in sys.modules


class TestServerImportBoundaries:
    def test_server_does_not_import_pyside_or_sounddevice(self) -> None:
        server_dir = _SRC / "server"
        if not server_dir.exists():
            pytest.skip("server/ ディレクトリ不在")
        for pyfile, first_parts, _full_names in _gather_imports(server_dir):
            for forbidden in _SERVER_FORBIDDEN:
                assert forbidden not in first_parts, (
                    f"{pyfile.relative_to(_SRC)} imports {forbidden}"
                )


class TestCommonImportBoundaries:
    def test_common_does_not_import_forbidden(self) -> None:
        common_dir = _SRC / "common"
        if not common_dir.exists():
            pytest.skip("common/ ディレクトリ不在")
        for pyfile, first_parts, full_names in _gather_imports(common_dir):
            for forbidden in _COMMON_FORBIDDEN:
                assert forbidden not in first_parts, (
                    f"{pyfile.relative_to(_SRC)} imports {forbidden}"
                )


class TestLazyImports:
    def test_opuslib_not_top_level_in_common(self) -> None:
        common_dir = _SRC / "common"
        if not common_dir.exists():
            pytest.skip("common/ ディレクトリ不在")
        for pyfile in sorted(common_dir.rglob("*.py")):
            try:
                tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
            except SyntaxError:
                continue
            for mod in _LAZY_IMPORTS:
                assert not _is_top_level_import(tree, mod), (
                    f"{pyfile.relative_to(_SRC)} top-level imports {mod}"
                )

    def test_soxr_not_top_level_in_common(self) -> None:
        common_dir = _SRC / "common"
        if not common_dir.exists():
            pytest.skip("common/ ディレクトリ不在")
        for pyfile in sorted(common_dir.rglob("*.py")):
            try:
                tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
            except SyntaxError:
                continue
            for mod in _LAZY_IMPORTS:
                assert not _is_top_level_import(tree, mod), (
                    f"{pyfile.relative_to(_SRC)} top-level imports {mod}"
                )


class TestBindValidation:
    def test_loopback_no_token_ok(self) -> None:
        from text_to_shaberi.server.config import BindConfig, validate_bind

        validate_bind(BindConfig(host="127.0.0.1", token=""))

    def test_localhost_no_token_ok(self) -> None:
        from text_to_shaberi.server.config import BindConfig, validate_bind

        validate_bind(BindConfig(host="localhost", token=""))

    def test_external_no_token_raises(self) -> None:
        from text_to_shaberi.server.config import BindConfig, validate_bind

        with pytest.raises(ValueError, match="loopback"):
            validate_bind(BindConfig(host="0.0.0.0", token=""))

    def test_external_with_token_ok(self) -> None:
        from text_to_shaberi.server.config import BindConfig, validate_bind

        validate_bind(BindConfig(host="0.0.0.0", token="mytoken"))


class TestServerHealthz:
    def test_healthz_returns_200(self) -> None:
        from fastapi.testclient import TestClient

        from text_to_shaberi.server.ws_server import create_app

        client = TestClient(create_app())
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
