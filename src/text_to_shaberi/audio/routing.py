"""PulseAudio 仮想sink ルーティング（best-effort）。

`pactl` 経由で sink-input を特定し、指定 sink へ移動する。
環境依存（PipeWire/PulseAudio）のため、すべての失敗はログ出力のみでクラッシュしない。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)


def move_app_to_sink(sink_name: str, app_name: str, pid: int) -> int | None:
    """自プロセスの sink-input を sink_name へ移動。成功時は sink-input id、失敗時は None。

    best-effort: pactl 不在や sink 未検出は警告ログで None を返す。
    """
    if shutil.which("pactl") is None:
        logger.warning("[route] pactl が見つかりません → ルーティング不可")
        return None

    # sink 存在確認
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if sink_name not in result.stdout:
            logger.warning("[route] sink '%s' が見つかりません", sink_name)
            return None
    except Exception as exc:
        logger.warning("[route] sink一覧取得失敗: %s", exc)
        return None

    # sink-input 一覧から自プロセスを特定
    try:
        result = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception as exc:
        logger.warning("[route] sink-input一覧取得失敗: %s", exc)
        return None

    candidates: list[int] = []
    current_id: int | None = None
    current_props: dict[str, str] = {}

    for line in result.stdout.splitlines():
        m = re.match(r"^Sink Input #(\d+)", line)
        if m:
            # 前のブロックを評価
            _check_candidate(current_id, current_props, app_name, pid, candidates)
            current_id = int(m.group(1))
            current_props = {}
            continue
        if "=" in line and current_id is not None:
            key_val = line.strip().split("=", 1)
            if len(key_val) == 2:
                key = key_val[0].strip()
                val = key_val[1].strip().strip('"')
                current_props[key] = val

    # 最後のブロックを評価
    _check_candidate(current_id, current_props, app_name, pid, candidates)

    if not candidates:
        logger.warning("[route] sink-input未検出 (app=%s pid=%s)", app_name, pid)
        return None

    target_id = max(candidates)
    if len(candidates) > 1:
        logger.info("[route] 候補%d件 → 最新 #%d を採用", len(candidates), target_id)

    # 移動実行
    try:
        subprocess.run(
            ["pactl", "move-sink-input", str(target_id), sink_name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        logger.info("[route] sink-input #%d → %s に移動", target_id, sink_name)
        return target_id
    except Exception as exc:
        logger.warning("[route] move-sink-input 失敗: %s", exc)
        return None


def _check_candidate(
    sink_input_id: int | None,
    props: dict[str, str],
    app_name: str,
    pid: int,
    out: list[int],
) -> None:
    if sink_input_id is None:
        return
    if props.get("application.process.id") == str(pid) and props.get("application.name") == app_name:
        out.append(sink_input_id)
