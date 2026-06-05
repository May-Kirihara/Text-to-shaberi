"""test_text_segmenter: TextSegmenter の純ロジックテスト。"""
from __future__ import annotations

import random

from text_to_shaberi.client.audio.vad import StreamComplete, StreamStart
from text_to_shaberi.client.text_input import TextSegment, TextSegmenter


def _texts(events):
    return [e.text for e in events if isinstance(e, TextSegment)]


def test_stream_start_on_first_feed():
    s = TextSegmenter()
    ev = s.feed("あ")
    assert isinstance(ev[0], StreamStart)


def test_japanese_sentence_split():
    s = TextSegmenter()
    ev = s.feed("こんにちは。今日は晴れ。")
    assert _texts(ev) == ["こんにちは。", "今日は晴れ。"]


def test_closing_bracket_absorbed():
    s = TextSegmenter()
    ev = s.feed("「やあ。」そうだ。")
    assert _texts(ev) == ["「やあ。」", "そうだ。"]


def test_consecutive_terminals_absorbed():
    s = TextSegmenter()
    ev = s.feed("本当！？まじで。")
    assert _texts(ev) == ["本当！？", "まじで。"]


def test_decimal_not_split():
    s = TextSegmenter()
    ev = s.feed("円周率は3.14です。")
    assert _texts(ev) == ["円周率は3.14です。"]


def test_incremental_tokens():
    s = TextSegmenter()
    out = []
    for tok in ["こん", "にち", "は。き", "ょうは", "晴れ。"]:
        out += _texts(s.feed(tok))
    out += _texts(s.flush())
    assert out == ["こんにちは。", "きょうは晴れ。"]


def test_flush_emits_remainder_and_complete():
    s = TextSegmenter()
    s.feed("途中まで")
    ev = s.flush()
    assert _texts(ev) == ["途中まで"]
    assert isinstance(ev[-1], StreamComplete)


def test_hard_max_force_cut_no_char_lost():
    s = TextSegmenter(max_chars=10, hard_max_chars=12)
    long = "あ" * 30
    out = _texts(s.feed(long))
    out += _texts(s.flush())
    assert "".join(out) == long
    assert all(len(t) <= 12 for t in out[:-1])


def test_property_no_char_lost():
    rng = random.Random(0)
    corpus = "これはテスト。3.14は円周率！「引用。」終わり？\nつぎの段落。" * 5
    s = TextSegmenter(max_chars=8, hard_max_chars=16)
    out = []
    i = 0
    while i < len(corpus):
        step = rng.randint(1, 5)
        out += _texts(s.feed(corpus[i:i + step]))
        i += step
    out += _texts(s.flush())
    assert "".join(out) == corpus
