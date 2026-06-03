# X2 codex レビュー指摘（gpt-5.5・read-only・実コード突合）

> 対象: `docs/IMPL-X2.md` v1（5本柱）。総評: **条件付きGO**。「`WSClient` の接続ライフサイクルを先に再設計しないまま5本柱を同時に入れるのは危険」。
> **分割勧告**: X2a = reconnect + close/reconnect 整理、X2b = mic pause、X2c = settings GUI/store、X2d = metrics/error UI。各段で既存テスト green を条件に。
> **採否**: 分割勧告を採用。**X2a（再接続＋接続ライフサイクル再設計）を単独で先行**（critical 1-4）、残り（mic pause/全設定GUI＋型store/計測/エラーUI）を **X2b** にまとめる（2分割）。

## critical（主に X2a で対応）
1. **接続ライフサイクル多重化**: `connect()` が `_ws`/`_recv_task` を無条件上書き。`_reconnect_loop`/手動`reconnect()`/`speak/utter` 自動connect が重なると socket/recv loop 多重化。→ **`asyncio.Lock` で connect/reconnect/close 直列化**、`connect()` 冒頭「接続済み＆recv_task alive なら no-op」、handshake 失敗時は作成済み `_ws` を必ず close/None、`_recv_task` は `_recv_loop` の finally で自分自身なら None。
2. **close と reconnect の責務混在**: `close()` が `_user_closing=True` を立てる一方 `reconnect()` を「close相当」と書いた→手動reconnectが自動再接続を殺すか close が止まり切らない。→ **公開 `close()`（user close）と内部 `_teardown_ws(user_closing)` を分離**。`reconnect()` = `_cancel_reconnect_task()`＋`_teardown_ws(False)`＋`_apply_server_settings()`＋`connect()`。
3. **_recv_loop から再接続スケジュール時の self-task 矛盾**: schedule 時点で `_recv_task` は生存中＝「alive なら弾く」と自己矛盾。→ `_recv_loop` finally で `if self._recv_task is current_task(): self._recv_task=None`、その後 `loop.call_soon(self._schedule_reconnect)`。
4. **未接続判定が `_ws is None` のみ**: 切断済みで `_ws` 残存だと speak/utter が `_send` 失敗＋再接続ループと競合。→ **`is_connected()`**（`_ws` 有・未close・`_recv_task` alive・`_ready` 有）を導入。送信失敗時は active 解放＋reconnect schedule。
5. **busy/idle と mic pause/resume 配線が粗い（X2b）**: `idle` は cancel/切断/error/空transcript/TTS終了の全経路で来る。「トグルON のまま」だけで resume すると停止直後/設定変更中の MicCapture に触る競合。→ `mic_enabled_by_user`/`mic_holder is not None`/`mic.active`/`not shutting_down` を条件化、設定変更で破棄前に pause/resume 経路を無効化するフラグ。

## should
6. **mic pause 後の flush 尾**（X2b）: 停止時 `flush()` が pause 直後の尾を emit すると目的を壊す。→ pause reset 後は flush しない／`_discard_tail` で stop 時 tail emit 抑制＋テスト。
7. **settings_store の bool/int＋演算子優先順位**（X2b）: bool は int サブクラス＝`isinstance(v,bool)` を**先に**。既存 `if found and stripped_j.startswith("#") or stripped_j == ""` は優先順位バグ→`if found and (... or ...):`。`[mic.vad]` 既存キー置換/追記をテスト固定。
8. **settings_dialog `values()` 契約**（X2b）: 旧フラット→型付き nested で app 契約が曖昧。→ `values()` を最初から `{"server":..,"mic":..,"mic.vad":..,"mic.preprocess":..,"output":..,"profile":..}` の `update_client_toml` 互換ドット section 形に。
9. **output.device が combo 初期選択に未反映**（X2b）: 保存値と `selected_output_device()` がズレる。→ `MainWindow.set_output_device(value)`＋初期選択（名前/index/default/__ttsvirt__ 明示）。
10. **`ServerConfig` に reconnect 項目未定義**（X2a）: toml に書いても `_only` で読み捨て。→ `ServerConfig(reconnect: bool=True, reconnect_max_delay: float=30.0)` 追加、WSClient の auto_reconnect/max_delay/base_delay 注入へ。
11. **metrics の未観測値/経路差**（X2b）: speak には transcript 無し、`utter(auto_tts=False)` は TTFA 無し→UI が古い値を残す。→ payload に `kind:"speak"|"utter"`、未観測は `None`、stream start で該当 metric クリア。
12. **Qt テストの QApplication 共有**（X2b）: 各テスト生成は CI 不安定。→ `tests/conftest.py` に `QT_QPA_PLATFORM=offscreen`＋session scope `qapp` fixture、widget 初期値/values 検証に限定。
13. **既存 WSClient テストの自動再接続**（X2a）: 既定 ON だとサーバ終了後 background task 残留。→ **既存テストの WSClient 生成を `auto_reconnect=False` に一括更新**、再接続専用テストのみ短 delay＋fake jitter で ON。

## nit
14. **task 生成の混在**（X2a）: `ensure_future` の loop 引数あり/なし混在。→ `self._loop.create_task(...)` に寄せ、qasync loop 設定後にのみ呼ぶ前提を明文化。
15. **状態 payload 仕様の表**（X2a/b）: `connected/disconnected/reconnecting/error/metrics` の payload を小さく表に。UI は表示するだけ。
16. **5本柱が過大**: → **分割**（採用・上記）。

## 反映方針
- **X2a（本先行）**: critical 1-4＋should#10＋nit#13,14,15。`WSClient` の接続ライフサイクルを `asyncio.Lock` 直列化・`_teardown_ws`/`close`/`reconnect`/`is_connected`/`_recv_loop` finally・自動再接続(backoff+jitter, config化, 注入OFF) で**綺麗に再設計**。既存テストは `auto_reconnect=False` 化。
- **X2b（後続）**: critical#5＋should 6,7,8,9,11,12＋nit#15。mic pause/resume・全設定GUI＋型 settings_store・計測・エラー/状態UI・output combo 反映。
