"""新ストリームプロトコルメッセージの parse_message 往復テスト（§5.3）。"""
from __future__ import annotations

import json

from text_to_shaberi.common.protocol import (
    SegBegin,
    SegEnd,
    StreamBegin,
    StreamEnd,
    StreamStopped,
    Transcript,
    parse_message,
)


class TestStreamMessages:
    def test_stream_begin(self) -> None:
        msg = parse_message({"type": "stream_begin", "id": "s", "auto_tts": True})
        assert isinstance(msg, StreamBegin)
        assert msg.id == "s"
        assert msg.auto_tts is True

    def test_seg_begin(self) -> None:
        msg = parse_message({"type": "seg_begin", "id": "s", "index": 2})
        assert isinstance(msg, SegBegin)
        assert msg.index == 2
        assert msg.prev_tail == ""

    def test_seg_end(self) -> None:
        msg = parse_message({"type": "seg_end", "id": "s", "index": 2})
        assert isinstance(msg, SegEnd)
        assert msg.index == 2

    def test_stream_end(self) -> None:
        msg = parse_message({"type": "stream_end", "id": "s"})
        assert isinstance(msg, StreamEnd)
        assert msg.id == "s"

    def test_stream_stopped(self) -> None:
        msg = parse_message({"type": "stream_stopped", "id": "s"})
        assert isinstance(msg, StreamStopped)
        assert msg.id == "s"

    def test_transcript_with_index(self) -> None:
        msg = parse_message(
            {"type": "transcript", "id": "s", "index": 3, "text": "x", "final": True}
        )
        assert isinstance(msg, Transcript)
        assert msg.index == 3
        assert msg.text == "x"

    def test_transcript_index_default_none(self) -> None:
        msg = Transcript(id="s", text="hi")
        assert msg.index is None


class TestRoundtrip:
    def test_stream_begin_roundtrip(self) -> None:
        orig = StreamBegin(id="abc", auto_tts=False)
        parsed = parse_message(json.loads(orig.model_dump_json()))
        assert isinstance(parsed, StreamBegin)
        assert parsed.id == "abc"
        assert parsed.auto_tts is False

    def test_seg_begin_roundtrip(self) -> None:
        orig = SegBegin(id="x", index=5, prev_tail="foo")
        parsed = parse_message(json.loads(orig.model_dump_json()))
        assert isinstance(parsed, SegBegin)
        assert parsed.index == 5
        assert parsed.prev_tail == "foo"

    def test_seg_end_roundtrip(self) -> None:
        orig = SegEnd(id="x", index=5)
        parsed = parse_message(json.loads(orig.model_dump_json()))
        assert isinstance(parsed, SegEnd)
        assert parsed.index == 5

    def test_stream_end_roundtrip(self) -> None:
        orig = StreamEnd(id="z")
        parsed = parse_message(json.loads(orig.model_dump_json()))
        assert isinstance(parsed, StreamEnd)
        assert parsed.id == "z"

    def test_stream_stopped_roundtrip(self) -> None:
        orig = StreamStopped(id="z")
        parsed = parse_message(json.loads(orig.model_dump_json()))
        assert isinstance(parsed, StreamStopped)
        assert parsed.id == "z"

    def test_transcript_index_roundtrip(self) -> None:
        orig = Transcript(id="s", text="hi", index=7, final=True)
        parsed = parse_message(json.loads(orig.model_dump_json()))
        assert isinstance(parsed, Transcript)
        assert parsed.index == 7
