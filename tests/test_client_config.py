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


def test_input_config_from_toml(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[input]
enabled = true
host = "127.0.0.1"
port = 8771
token = "sek"
seg_max_chars = 60
seg_hard_max_chars = 120
""")
    cfg = load_client_config(str(p))
    assert cfg.input.enabled is True
    assert cfg.input.port == 8771
    assert cfg.input.token == "sek"
    assert cfg.input.seg_max_chars == 60
    assert cfg.input.seg_hard_max_chars == 120


def test_input_defaults(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, "")
    cfg = load_client_config(str(p))
    assert cfg.input.enabled is False
    assert cfg.input.host == "127.0.0.1"
    assert cfg.input.port == 8770
    assert cfg.input.forward_audio is False


def test_input_nonloopback_without_token_disabled(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[input]
enabled = true
host = "0.0.0.0"
""")
    cfg = load_client_config(str(p))
    assert cfg.input.enabled is False


def test_input_hard_max_clamp(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[input]
seg_max_chars = 100
seg_hard_max_chars = 40
""")
    cfg = load_client_config(str(p))
    assert cfg.input.seg_hard_max_chars >= cfg.input.seg_max_chars


def test_input_forward_audio(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[input]
forward_audio = true
""")
    cfg = load_client_config(str(p))
    assert cfg.input.forward_audio is True


def test_monitor_defaults(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, "")
    cfg = load_client_config(str(p))
    assert cfg.monitor.enabled is False
    assert cfg.monitor.host == "127.0.0.1"
    assert cfg.monitor.port == 8771
    assert cfg.monitor.queue_max == 256


def test_output_muted_default_and_from_toml(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, "")
    assert load_client_config(str(p)).output.muted is False
    _write_toml(p, """
[output]
muted = true
""")
    assert load_client_config(str(p)).output.muted is True


def test_monitor_config_from_toml(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[monitor]
enabled = true
port = 9001
queue_max = 64
""")
    cfg = load_client_config(str(p))
    assert cfg.monitor.enabled is True
    assert cfg.monitor.port == 9001
    assert cfg.monitor.queue_max == 64


def test_monitor_nonloopback_without_token_disabled(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[monitor]
enabled = true
host = "0.0.0.0"
""")
    cfg = load_client_config(str(p))
    assert cfg.monitor.enabled is False


def test_monitor_queue_max_clamp(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[monitor]
queue_max = 0
""")
    cfg = load_client_config(str(p))
    assert cfg.monitor.queue_max == 1


def test_caption_defaults(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, "")
    cfg = load_client_config(str(p))
    assert cfg.caption.enabled is False
    assert cfg.caption.host == "127.0.0.1"
    assert cfg.caption.port == 8772
    assert cfg.caption.token == ""
    assert cfg.caption.template == ""
    assert cfg.caption.max_chars == 200


def test_caption_config_from_toml(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[caption]
enabled = true
port = 9100
max_chars = 80
template = "/tmp/my_caption.html"
""")
    cfg = load_client_config(str(p))
    assert cfg.caption.enabled is True
    assert cfg.caption.port == 9100
    assert cfg.caption.max_chars == 80
    assert cfg.caption.template == "/tmp/my_caption.html"


def test_caption_nonloopback_without_token_disabled(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[caption]
enabled = true
host = "0.0.0.0"
""")
    cfg = load_client_config(str(p))
    assert cfg.caption.enabled is False


def test_caption_max_chars_clamp(tmp_path):
    p = tmp_path / "client.toml"
    _write_toml(p, """
[caption]
max_chars = 0
""")
    cfg = load_client_config(str(p))
    assert cfg.caption.max_chars == 1
