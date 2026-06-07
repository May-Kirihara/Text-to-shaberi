# SRT台本再生機能

## 概要

SRT字幕ファイルを読み込んでタイムライン通りに逐次発話する機能。

## アーキテクチャ

### 新規モジュール

- `client/srt_parser.py`: SRTパーサー。`parse_srt()` / `load_srt_file()`。HTMLタグ・制御タグ除去。UTF-8→Shift-JISフォールバック。
- `client/srt_player.py`: 再生スケジューラ。逐次speak + ギャップ調整。pause/resume/stop。仮想時計方式。
- `client/ui/srt_tab.py`: 台本タブUI。ファイル選択・テーブル・再生制御ボタン・プログレスバー。

### 変更モジュール

- `client/app.py`: MainWindowをQTabWidgetでラップ（「メイン」+「台本」）。SrtPlayer配線。セッションレベル排他。

## 再生方式

逐次speak + ギャップ調整。前の発話AudioEnd → 次のstartまでの差分をsleep → speak。

- **startのみ使用**: end_msは保存するが再生制御には使用しない
- **仮想時計**: `virtual_t = loop.time() - paused_cumulative - t_origin`
- **AudioEnd検知**: `on_state("idle")` → `asyncio.Event.set()`
- **排他**: SRT再生中は `_srt_playing=True`。idle時の`window.set_busy(False)`をスキップ。

## テスト

- `tests/test_srt_parser.py`: 17件（正常/異常/境界）
- `tests/test_srt_player.py`: 9件（逐次/制御/pause-resume）
- `tests/test_srt_tab.py`: 10件（初期状態/エントリ/再生状態）

全55テストパス。既存テスト回帰なし。
