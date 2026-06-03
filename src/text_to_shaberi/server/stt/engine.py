"""STT エンジン抽象 + Qwen3-ASR 実装（S2）。

torch / qwen_asr は __init__ 内で遅延 import（AST 境界テスト対策）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from ..config import QwenLocalConfig

_LANG_MAP: dict[str, str] = {
    "ja": "Japanese",
    "en": "English",
    "zh": "Chinese",
}


@runtime_checkable
class STTEngine(Protocol):
    def transcribe(self, samples: np.ndarray, sample_rate: int, language: str) -> str: ...


class QwenSTTEngine:
    def __init__(self, cfg: QwenLocalConfig) -> None:
        import torch
        from qwen_asr import Qwen3ASRModel

        _DTYPE_MAP: dict[str, "torch.dtype"] = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        dtype = _DTYPE_MAP[cfg.dtype]
        self._model = Qwen3ASRModel.from_pretrained(
            cfg.model,
            dtype=dtype,
            device_map=cfg.device,
            attn_implementation=cfg.attn_implementation,
            max_new_tokens=256,
        )

    def transcribe(self, samples: np.ndarray, sample_rate: int, language: str) -> str:
        lang = _LANG_MAP.get(language, language)
        res = self._model.transcribe(audio=(samples, sample_rate), language=lang)
        return res[0].text if res else ""
