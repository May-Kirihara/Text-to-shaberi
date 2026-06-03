"""共通設定ローダ helper（_only / tomllib ロード）。"""
from __future__ import annotations

import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any


def _only(d: dict[str, Any], cls: type) -> dict[str, Any]:
    """dataclass のフィールドに存在するキーだけ抽出（未知キーは無視）。"""
    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in valid}


def load_toml(path: str | Path) -> dict[str, Any]:
    """tomllib で TOML ファイルを読み込む。"""
    path = Path(path)
    with path.open("rb") as f:
        return tomllib.load(f)
