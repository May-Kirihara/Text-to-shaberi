"""TTSプロファイル (profiles/tts/*.toml) のロードとテンプレ展開。

`{{ var }}` を変数で置換する。値全体が単一プレースホルダの場合は変数の型を保持する
（例: streaming_mode のような数値を壊さない）。将来 Jinja2 等へ拡張可能な薄い実装。
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_value(value: Any, variables: dict[str, Any]) -> Any:
    """テンプレ値を再帰的に展開。単一プレースホルダは型を保持、混在文字列は文字列化。"""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole is not None:
            return variables.get(whole.group(1), value)
        return _PLACEHOLDER.sub(
            lambda m: str(variables.get(m.group(1), m.group(0))), value
        )
    if isinstance(value, dict):
        return {k: render_value(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, variables) for v in value]
    return value


@dataclass(slots=True)
class AudioFormat:
    encoding: str = "pcm_s16le"
    sample_rate: int = 32000
    channels: int = 1


@dataclass(slots=True)
class TTSProfile:
    name: str
    driver: str
    method: str
    url: str
    body_type: str
    timeout_sec: float
    headers: dict[str, str]
    body: dict[str, Any]
    response_mode: str
    response_content_type: str
    audio: AudioFormat
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "TTSProfile":
        path = Path(path)
        with path.open("rb") as f:
            raw = tomllib.load(f)
        meta = raw.get("meta", {})
        req = raw.get("request", {})
        resp = raw.get("response", {})
        audio = raw.get("audio", {})
        if "url" not in req:
            raise ValueError(f"TTSプロファイルに request.url がありません: {path}")
        return cls(
            name=meta.get("name", path.stem),
            driver=meta.get("driver", "generic_http"),
            method=req.get("method", "POST"),
            url=req["url"],
            body_type=req.get("body_type", "json"),
            timeout_sec=float(req.get("timeout_sec", 30)),
            headers=dict(req.get("headers", {})),
            body=dict(req.get("body", {})),
            response_mode=resp.get("mode", "audio_stream"),
            response_content_type=resp.get("content_type", ""),
            audio=AudioFormat(
                encoding=audio.get("encoding", "pcm_s16le"),
                sample_rate=int(audio.get("sample_rate", 32000)),
                channels=int(audio.get("channels", 1)),
            ),
            path=path,
        )

    def render_body(self, text: str, **extra: Any) -> dict[str, Any]:
        """共通テキスト（と追加変数）でリクエストbodyを展開する。"""
        return render_value(self.body, {"text": text, **extra})


def load_profile(profiles_dir: str | Path, name: str) -> TTSProfile:
    """profiles_dir/tts/<name>.toml をロードする。"""
    return TTSProfile.load(Path(profiles_dir) / "tts" / f"{name}.toml")
