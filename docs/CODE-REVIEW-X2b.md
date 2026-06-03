# X2b コードレビュー / 検証記録

> 対象: X2b「マイクpause ＋ 全設定GUI ＋ 型store ＋ 計測 ＋ エラーUI」（GLM-5.1 実装 → 私が検証・統合・修正）。
> 仕様: `docs/IMPL-X2b.md` v1（codex `docs/REVIEW-codex-X2.md` の X2 分割勧告の後半＝critical#5＋should 6,7,8,9,11,12＋nit#12,15）。#13「設定メニュー」を内包。

## 1. 実装サマリ（GLM）
| 対象 | 内容 | 判定 |
|---|---|---|
| `mic.py` pause/resume | `_paused`(Event)/`_reset_pending`/`_discard_tail`・worker は pause 中 read 継続＋フレーム破棄＋reset 1回・**pause 中 stop で flush 尾を emit しない** | ✅ critical#5/should#6 |
| `settings_store.py` 型対応 | `_toml_value`（**bool を int より先に判定**・int/float/str）・**演算子優先順位修正** `if found and (... or ...):` | ✅ should#7 |
| `settings_dialog.py` 全項目 | QGroupBox×3（サーバ/マイク/出力）・`values()` が **update_client_toml 互換のドット section 型付き dict**（server/profile/mic/mic.vad/mic.preprocess/output、tls_insecure=bool・aggressiveness=int・gate=bool） | ✅ should#8 |
| `ws_client.py` 計測 | `on_state("metrics", {kind, ttfa_ms, stt_ms, codec})`・speak/utter 開始で None クリア・transcript で stt_ms・初音で ttfa_ms・`_apply_server_settings` setter | ✅ should#11 |
| `main_window.py` | `set_status`（ステータス行 QLabel）・`set_output_device`（default/__ttsvirt__/index/name）・`_on_state` 反映先 | ✅ should#9/nit#15 |
| `app.py` 配線 | `mic_enabled_by_user`/`shutting_down`/`mic_reconfiguring` フラグ・busy→pause/idle→resume（全ガード AND）・on_settings（update_client_toml→cfg更新→`_apply_server_settings`＋reconnect／mic 再生成／set_output_device）・metrics→set_status・error→set_status | ✅ critical#5 |
| `tests/conftest.py` | `QT_QPA_PLATFORM=offscreen`＋session `qapp` fixture | ✅ nit#12 |
| tests | test_mic_pause/test_settings_dialog/test_main_window 新規・test_settings_store 型/test_ws_client metrics 追加 | ✅ |

## 2. 私による修正
- **`main_window.set_output_device(int)` の `AttributeError`**: `int(value)` 成功も `findData(idx)` 不一致時に名前マッチへ落ち `value.lower()` で crash（`client.toml` に `device = 3`＝int を書くと到達）。→ 名前マッチ前に **`if not isinstance(value, str): return`** を追加（int index で該当無し＝名前マッチ不要）。**offscreen GUI スモークで検出**（ユニットが str のみ検証だと見逃す型・C2 の「GUI 専用バグはユニットをすり抜ける」教訓の再現）。

## 3. 検証結果
- **ユニット**: `uv run pytest -q` **209 passed**（X2a の165 + X2b の mic_pause/settings_dialog/main_window/型 settings_store/metrics）。私の修正後も green。複数回クリーン。
- **検証手順上の注意（GLM の遅延編集）**: 150s 静止ウォッチャが GLM の長い推論ポーズで**早期発火**し、私が回した pytest は **mid-edit の中間状態**（GLM が FakeStream→BlockingFakeStream に書き換え中）で 5 失敗を表示した。**手を出さず**、200s 静止＋opencode 子プロセス無し条件で再武装して真の完了を待った結果、GLM 自身が修正完了し **209 passed**（mid-edit の失敗に介入しなかったのが正解）。
- **offscreen GUI スモーク** `/tmp/x2b_gui_smoke.py`（run_gui 配線相当を offscreen で）:
  - MainWindow の X2b API（set_status/set_output_device(default/__ttsvirt__/int)/set_mic_active/enabled/set_busy）全て無例外。
  - `_on_state` を **全イベント**（ready/connected/busy/reconnecting/transcript/metrics×2/error/idle/disconnected）で叩き **AttributeError 無し**（run_gui-class の配線バグを検出する設計）。
  - `SettingsDialog.values()` が型付きドット section（mic.vad=int/mic.preprocess=bool/server.tls_insecure=bool）。
  - **往復 dialog→update_client_toml→reload で型保持**（tls_insecure=bool・aggressiveness=int・gate=bool）。RESULT PASS。
- **境界/コンパイル**: AST 境界 green・py_compile OK・既存全テスト（C1/C2/S2/X1/X2a）不変で green。

## 4. 後続・未対応
- **マイク pause の実機体感**（あなた）: 再生中にマイクが止まる/idle で再開（フィードバック抑止）の Windows/Linux 実機確認。
- **本格 AEC/バージイン・デバイスホットスワップ**: スコープ外（将来）。
- **計測の実サーバ e2e**: metrics 計算（transcript→stt_ms、初音→ttfa_ms）は既検証イベント上の算術で、ユニット＋GUI スモークで担保。実 STT/auto_tts 経路は S2/C2/X1 で実証済み。

## 5. これで「仕上げ」完了
S1→C1→S2→C2→X1→**X2a→X2b** で、テキスト/マイク→STT→auto_tts→opus再生＋token/wss＋自動再接続＋全設定GUI＋計測＋エラーUI が揃い、日常運用に耐える状態。#13（設定メニュー）も X2b に内包。
