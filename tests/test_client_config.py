"""test_client_config: load_client_config の検証（seg_*・stream_mode・クランプ）。"""
from __future__ import annotations


from text_to_shaberi.client.config import load_client_config


def _write_toml(path, text):
    path.write_text(text, encoding="utf-8")


def test_seg_fields_from_toml(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[mic.vad]
seg_silence_ms = 300
end_silence_ms = 700
seg_min_ms = 500
seg_max_ms = 3000
""")
    cfg = load_client_config(str(p))
    assert cfg.mic.vad.seg_silence_ms == 300
    assert cfg.mic.vad.end_silence_ms == 700
    assert cfg.mic.vad.seg_min_ms == 500
    assert cfg.mic.vad.seg_max_ms == 3000


def test_stream_mode_from_toml(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[mic]
stream_mode = true
""")
    cfg = load_client_config(str(p))
    assert cfg.mic.stream_mode is True


def test_clamp_end_silence_ge_seg_silence(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[mic.vad]
seg_silence_ms = 400
end_silence_ms = 100
frame_ms = 30
""")
    cfg = load_client_config(str(p))
    floor = 400 + 2 * 30
    assert cfg.mic.vad.end_silence_ms >= floor


def test_clamp_seg_max_ge_seg_min(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[mic.vad]
seg_min_ms = 2000
seg_max_ms = 500
""")
    cfg = load_client_config(str(p))
    assert cfg.mic.vad.seg_max_ms >= cfg.mic.vad.seg_min_ms


def test_defaults(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, "")
    cfg = load_client_config(str(p))
    assert cfg.mic.vad.seg_silence_ms == 250
    assert cfg.mic.vad.end_silence_ms == 500
    assert cfg.mic.vad.seg_min_ms == 400
    assert cfg.mic.vad.seg_max_ms == 2500
    assert cfg.mic.stream_mode is False
