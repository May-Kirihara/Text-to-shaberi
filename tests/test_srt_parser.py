"""test_srt_parser: SRT パーサーのユニットテスト。

対象: text_to_shaberi.client.srt_parser（未実装・TDD）
"""
from __future__ import annotations

from text_to_shaberi.client.srt_parser import SubtitleEntry, parse_srt, load_srt_file


_THREE_ENTRY_SRT = """\
1
00:00:01,000 --> 00:00:04,000
Hello

2
00:00:05,000 --> 00:00:08,000
World

3
00:00:10,000 --> 00:00:13,500
End
"""

_MULTILINE_SRT = """\
1
00:00:01,000 --> 00:00:04,000
Line one
Line two

2
00:00:05,000 --> 00:00:08,000
Single
"""


class TestParseSrt:
    def test_three_entries(self):
        entries = parse_srt(_THREE_ENTRY_SRT)
        assert len(entries) == 3
        assert entries[0] == SubtitleEntry(index=1, start_ms=1000, end_ms=4000, text="Hello")
        assert entries[1] == SubtitleEntry(index=2, start_ms=5000, end_ms=8000, text="World")
        assert entries[2] == SubtitleEntry(index=3, start_ms=10000, end_ms=13500, text="End")

    def test_multiline_text_joined(self):
        entries = parse_srt(_MULTILINE_SRT)
        assert len(entries) == 2
        assert entries[0].text == "Line one\nLine two"

    def test_zero_timestamp(self):
        srt = "1\n00:00:00,000 --> 00:00:01,000\nText\n"
        entries = parse_srt(srt)
        assert entries[0].start_ms == 0

    def test_large_timestamp(self):
        srt = "1\n01:30:45,500 --> 01:30:50,000\nText\n"
        entries = parse_srt(srt)
        assert entries[0].start_ms == 5445500

    def test_bom_utf8(self):
        srt = "\ufeff1\n00:00:01,000 --> 00:00:02,000\nBOM\n"
        entries = parse_srt(srt)
        assert len(entries) == 1
        assert entries[0].text == "BOM"

    def test_html_tags_removed(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n<i>italic</i>\n"
        entries = parse_srt(srt)
        assert entries[0].text == "italic"

    def test_html_bold_removed(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n<b>bold</b>\n"
        entries = parse_srt(srt)
        assert entries[0].text == "bold"

    def test_html_font_removed(self):
        srt = '1\n00:00:01,000 --> 00:00:02,000\n<font color="white">t</font>\n'
        entries = parse_srt(srt)
        assert entries[0].text == "t"

    def test_srt_control_tag_removed(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n{\\an8}\n"
        entries = parse_srt(srt)
        assert entries[0].text == ""

    def test_srt_pos_tag_removed(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n{\\pos(100,200)}\n"
        entries = parse_srt(srt)
        assert entries[0].text == ""

    def test_empty_input(self):
        entries = parse_srt("")
        assert entries == []

    def test_invalid_timestamp_skips_entry(self):
        srt = "1\nINVALID --> 00:00:02,000\nText\n"
        entries = parse_srt(srt)
        assert entries == []

    def test_index_gap_accepted(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\nA\n\n5\n00:00:03,000 --> 00:00:04,000\nB\n"
        entries = parse_srt(srt)
        assert len(entries) == 2
        assert entries[0].index == 1
        assert entries[1].index == 5

    def test_start_equals_end(self):
        srt = "1\n00:00:01,000 --> 00:00:01,000\nZero\n"
        entries = parse_srt(srt)
        assert len(entries) == 1
        assert entries[0].start_ms == entries[0].end_ms

    def test_crlf_line_endings(self):
        srt = "1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nWorld\r\n"
        entries = parse_srt(srt)
        assert len(entries) == 2
        assert entries[0].text == "Hello"
        assert entries[1].text == "World"

    def test_start_after_end(self):
        srt = "1\n00:00:05,000 --> 00:00:01,000\nReversed\n"
        entries = parse_srt(srt)
        assert len(entries) == 1
        assert entries[0].start_ms == 5000
        assert entries[0].end_ms == 1000


class TestLoadSrtFile:
    def test_load_from_file(self, tmp_path):
        p = tmp_path / "test.srt"
        p.write_text(_THREE_ENTRY_SRT, encoding="utf-8")
        entries = load_srt_file(str(p))
        assert len(entries) == 3

    def test_load_bom_file(self, tmp_path):
        p = tmp_path / "bom.srt"
        p.write_text("\ufeff1\n00:00:01,000 --> 00:00:02,000\nBOM\n", encoding="utf-8-sig")
        entries = load_srt_file(str(p))
        assert len(entries) == 1
        assert entries[0].text == "BOM"
