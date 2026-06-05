from text_to_shaberi.common.protocol import (
    StreamBegin,
    SegText,
    _MESSAGE_TYPES,
    parse_message,
)


def test_stream_begin_default_mode():
    m = StreamBegin(id="x")
    assert m.mode == "audio"
    assert m.profile is None


def test_stream_begin_text_mode_roundtrip():
    m = StreamBegin(id="x", mode="text", profile="p1")
    m2 = parse_message(m.model_dump())
    assert isinstance(m2, StreamBegin)
    assert m2.mode == "text"
    assert m2.profile == "p1"


def test_seg_text_parse():
    m = parse_message({"type": "seg_text", "id": "x", "index": 2, "text": "こんにちは"})
    assert isinstance(m, SegText)
    assert m.index == 2
    assert m.text == "こんにちは"


def test_seg_text_registered():
    assert _MESSAGE_TYPES.get("seg_text") is SegText
