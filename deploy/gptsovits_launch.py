"""GPT-SoVITS api_v2 ランチャ（プレビルドイメージの torchcodec 破損を回避）。

背景:
  xxxxrt666/gpt-sovits:latest-cu128 は torchaudio 2.x が音声デコードを torchcodec
  経由に固定する一方、同梱の torchcodec が torch (2.10.0+cu128) と ABI 不一致
  (`undefined symbol: torch_from_blob`)。さらに conda 側は libavutil.so.60(FFmpeg8)
  しか持たず、torchcodec の v4〜v7 は libav* 不在でロード不可。結果 torchaudio.load が
  全滅し、参照wav(`ref_audio_path`)の読み込みで api_v2 が 500 を返す。

対処:
  torchaudio.load を soundfile 実装に差し替えてから api_v2 を本来の引数で起動する。
  GPT-SoVITS は参照wavを読むだけなので soundfile(libsndfile) で十分・確実。
  イメージ再ビルドも GPT-SoVITS 本体の改変も不要（このファイルを read-only マウントするだけ）。

注: soundfile が無い等で差し替えに失敗した場合は torchaudio をそのまま使う（=元の挙動）。
"""
from __future__ import annotations

import runpy
import sys

try:
    import soundfile as sf
    import torch
    import torchaudio

    def _load_via_soundfile(path, *args, **kwargs):
        # torchaudio.load 互換: float32 [-1,1] / shape=(channels, frames) を返す
        data, sr = sf.read(path, dtype="float32", always_2d=True)  # (frames, channels)
        return torch.from_numpy(data.T).contiguous(), sr           # (channels, frames)

    torchaudio.load = _load_via_soundfile
    print("[shaberi-launch] torchaudio.load -> soundfile に差し替え済み", flush=True)
except Exception as exc:  # noqa: BLE001 - 差し替え不可でも api_v2 は起動を試みる
    print(f"[shaberi-launch] soundfile パッチ不可（元の torchaudio のまま続行）: {exc!r}", flush=True)

# api_v2 の argparse にそのまま渡す（compose の command と同じ）
sys.argv = ["api_v2.py", "-a", "0.0.0.0", "-p", "9880", "-c", "GPT_SoVITS/configs/tts_infer.yaml"]
# working_dir=/workspace/GPT-SoVITS 前提（相対で api_v2.py / configs を解決）
runpy.run_path("api_v2.py", run_name="__main__")
