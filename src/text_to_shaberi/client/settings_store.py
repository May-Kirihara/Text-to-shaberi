"""client.toml の scalar 文字列キーをコメント保持で in-place 更新。

行指向で既存行を置換し、section/key が無ければ末尾に追記。
原子的書き込み（一時ファイル→os.replace）＋ tomllib 検証。
"""
from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_ARRAY_TABLE_RE = re.compile(r"^\s*\[\[")


def _toml_str(v: str) -> str:
    v = v.replace("\\", "\\\\")
    v = v.replace('"', '\\"')
    v = v.replace("\n", "\\n")
    v = v.replace("\r", "\\r")
    v = v.replace("\t", "\\t")
    result: list[str] = []
    for ch in v:
        cp = ord(ch)
        if cp <= 0x1F:
            result.append(f"\\u{cp:04x}")
        else:
            result.append(ch)
    return '"' + "".join(result) + '"'


def update_client_toml(path: str | Path, updates: dict[str, dict[str, str]]) -> None:
    path = Path(path)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(True)
    else:
        lines: list[str] = []

    current_section: str | None = None
    written_keys: set[tuple[str, str]] = set()

    new_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        m = _SECTION_RE.match(stripped)
        if m and not _ARRAY_TABLE_RE.match(stripped):
            current_section = m.group(1)
            new_lines.append(line)
            i += 1
            continue

        if stripped.startswith("#") or stripped == "":
            new_lines.append(line)
            i += 1
            continue

        if current_section is not None and "=" in stripped:
            key_part = stripped.split("=", 1)[0].strip()
            if current_section in updates and key_part in updates[current_section]:
                new_lines.append(f"{key_part} = {_toml_str(updates[current_section][key_part])}\n")
                written_keys.add((current_section, key_part))
                i += 1
                continue

        new_lines.append(line)
        i += 1

    for section, keys in updates.items():
        for key, value in keys.items():
            if (section, key) in written_keys:
                continue

            found = False
            for j, line in enumerate(new_lines):
                stripped_j = line.strip()
                m = _SECTION_RE.match(stripped_j)
                if m and not _ARRAY_TABLE_RE.match(stripped_j) and m.group(1) == section:
                    found = True
                    continue
                if found and _SECTION_RE.match(stripped_j):
                    new_lines.insert(j, f"{key} = {_toml_str(value)}\n")
                    written_keys.add((section, key))
                    found = False
                    break
                if found and stripped_j.startswith("#") or stripped_j == "":
                    continue
            else:
                if found:
                    new_lines.append(f"{key} = {_toml_str(value)}\n")
                    written_keys.add((section, key))
                    continue

                if not any(
                    _SECTION_RE.match(l.strip())
                    and not _ARRAY_TABLE_RE.match(l.strip())
                    and _SECTION_RE.match(l.strip()).group(1) == section
                    for l in new_lines
                    if _SECTION_RE.match(l.strip())
                ):
                    if new_lines and not new_lines[-1].endswith("\n"):
                        new_lines.append("\n")
                    elif new_lines and new_lines[-1].strip() != "":
                        new_lines.append("\n")
                    new_lines.append(f"[{section}]\n")
                    new_lines.append(f"{key} = {_toml_str(value)}\n")
                    written_keys.add((section, key))

    content = "".join(new_lines)
    tomllib.loads(content)

    dir_path = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
