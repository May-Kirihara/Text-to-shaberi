"""音声モニタ(8771)の既知良リスナー（問題切り分け用の診断プローブ）。

使い方:
  # 8771 を購読して受信を表示（別ターミナルで GUI送信/マイク/LLM で音声を出す）
  uv run python scripts/monitor_listen.py

  # 入力口(8770)が enabled なら、テキストを投げて音声をトリガしつつ受信も確認
  uv run python scripts/monitor_listen.py "こんにちは。テストです。"

  # ポート/ホストを変える
  uv run python scripts/monitor_listen.py --url ws://127.0.0.1:8771 --input ws://127.0.0.1:8770 "やあ。"

判定:
  AUDIO_BEGIN → chunk(...) → AUDIO_END が出れば【クライアント→モニタの送出は正常】
  → 音が来ないのは受信側の問題。
  何も来ない場合は、音声が合成されているか（GUIログに TTFA が出るか／ローカル再生されるか）
  と、購読接続ログ "[monitor] 購読者接続" がクライアント側に出ているかを確認。
"""
from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def _maybe_trigger(input_url: str, text: str) -> None:
    try:
        async with websockets.connect(input_url) as ws:
            await ws.send(json.dumps({"text": text, "final": True}, ensure_ascii=False))
            # accepted/done を少し待って表示
            for _ in range(3):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8.0)
                    print(f"[input] {msg}")
                except asyncio.TimeoutError:
                    break
    except Exception as exc:
        print(f"[input] トリガ失敗（[input].enabled? ポート?）: {exc}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default=None, help="あれば入力口へ送ってトリガ")
    ap.add_argument("--url", default="ws://127.0.0.1:8771", help="音声モニタ URL")
    ap.add_argument("--input", default="ws://127.0.0.1:8770", help="入力口 URL（トリガ用）")
    ap.add_argument("--timeout", type=float, default=30.0, help="受信待ち秒")
    args = ap.parse_args()

    print(f"[probe] connecting {args.url} ...")
    async with websockets.connect(args.url) as ws:
        print("[probe] connected. 音声を出してください（GUI送信/マイク/LLM）。Ctrl-C で停止。")
        if args.text:
            asyncio.create_task(_maybe_trigger(args.input, args.text))

        n_begin = n_chunk = n_end = 0
        total = 0
        while True:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            except asyncio.TimeoutError:
                print(f"[probe] {args.timeout:.0f}s 何も受信せず。"
                      f"（begin={n_begin} chunk={n_chunk} end={n_end} {total}B）")
                continue
            if isinstance(m, bytes):
                n_chunk += 1
                total += len(m)
                if n_chunk <= 3 or n_chunk % 50 == 0:
                    print(f"[probe] chunk #{n_chunk} {len(m)}B (total {total}B)")
            else:
                ev = json.loads(m)
                e = ev.get("event")
                if e == "audio_begin":
                    n_begin += 1
                    print(f"[probe] >>> AUDIO_BEGIN #{n_begin} format={ev.get('format')}")
                elif e == "audio_end":
                    n_end += 1
                    print(f"[probe] <<< AUDIO_END #{n_end} (このストリーム計 chunk={n_chunk}, {total}B)")
                else:
                    print(f"[probe] event: {ev}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[probe] stopped.")
