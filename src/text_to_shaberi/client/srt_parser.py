"""SRT 字幕パーサー。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SubtitleEntry:
    index: int
    start_ms: int
    end_ms: int
    text: str


_TS_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
_HTML_RE = re.compile(r"<[^>]+>")
_SRT_CTRL_RE = re.compile(r"\{\\[^}]+\}")


def _ts_to_ms(h: str, m: str, s: str, ms: str) -> int:
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def parse_srt(content: str) -> list[SubtitleEntry]:
    if content.startswith("\ufeff"):
        content = content[1:]
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    blocks = re.split(r"\n\n+", content.strip())
    entries: list[SubtitleEntry] = []

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        try:
            index = int(lines[0].strip())
        except ValueError:
            continue

        ts_match = _TS_RE.findall(lines[1])
        if len(ts_match) != 2:
            continue

        start_ms = _ts_to_ms(*ts_match[0])
        end_ms = _ts_to_ms(*ts_match[1])

        text = "\n".join(lines[2:])
        text = _SRT_CTRL_RE.sub("", text)
        text = _HTML_RE.sub("", text)
        text = text.strip()

        entries.append(SubtitleEntry(index=index, start_ms=start_ms, end_ms=end_ms, text=text))

    return entries


def load_srt_file(path: str) -> list[SubtitleEntry]:
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8-sig")
        return parse_srt(content)
    except (UnicodeDecodeError, ValueError):
        pass
    try:
        content = p.read_text(encoding="shift_jis")
        return parse_srt(content)
    except (UnicodeDecodeError, ValueError):
        return []
