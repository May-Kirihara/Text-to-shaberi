# X2a コードレビュー / 検証記録

> 対象: X2a「自動再接続 ＋ WSClient 接続ライフサイクル再設計」（GLM-5.1 実装 → 私が検証・統合・修正）。
> 仕様: `docs/IMPL-X2a.md` v1（codex `docs/REVIEW-codex-X2.md` の X2 分割勧告に基づく X2a 集中版＝critical 1-4＋should#10＋nit#13,14,15）。

## 1. 実装サマリ（GLM）
| 対象 | 内容 | 判定 |
|---|---|---|
| `ws_client.py` 接続ライフサイクル | `_conn_lock`(asyncio.Lock) で connect/reconnect/close 直列化・`connect()` 冒頭 `is_connected()` no-op・handshake 失敗で `_safe_close_ws`・`_recv_task=self._get_loop().create_task(...)` | ✅ critical#1 |
| close/reconnect 分離 | 公開 `close()`(=user close: `_user_closing=True`+`_cancel_reconnect_task`+lock teardown)・内部 `_teardown_ws()`・`reconnect()`(=cancel+lock teardown+**lock外** connect) | ✅ critical#2（非再入安全） |
| `_recv_loop` finally | self-task クリア(`is current_task`)→active中断→`disconnected`→`call_soon(_schedule_reconnect)` | ✅ critical#3（私が cancelled 判別を追加） |
| `is_connected()` | `_ws`有・`close_code` falsy・recv_task alive・`_ready`有。speak/utter の未接続判定に使用・送信失敗で `_schedule_reconnect` | ✅ critical#4 |
| 自動再接続 | `_schedule_reconnect`/`_reconnect_loop`（指数バックオフ `base*2^n` cap `max`＋`random.uniform` jitter・`reconnecting` state・`auto_reconnect`/`reconnect_base`/`reconnect_max_delay` 注入） | ✅ |
| config/app/toml | `ServerConfig`+reconnect/reconnect_max_delay・app が注入・`_on_state` "reconnecting" 分岐・`client.toml` 追記 | ✅ should#10 |
| tests | 既存 `test_ws_client.py` 全生成に `auto_reconnect=False`・`test_ws_reconnect.py`(6) | ✅ nit#13 |

`create_task` 統一(nit#14)・`on_state` payload 表(nit#15) も反映。

## 2. 私による修正
- **`_recv_loop` finally の幻 reconnecting**: GLM 実装は finally で `if not self._user_closing and self._auto_reconnect: call_soon(_schedule_reconnect)` としていた。CancelledError も finally を通るため、**手動 `reconnect()`（設定保存）が teardown で recv_task を cancel→`_user_closing=False` のまま finally が再接続をスケジュール**し、「再接続中…(1)」が一瞬出て 0.5s の幻タスクが走る（connect no-op で自滅するが UX グリッチ）。→ **`cancelled` フラグを導入**し「CancelledError(=自分の teardown 由来)」のみ再接続を抑止。**サーバ正常切断(async for 正常終了)/ConnectionClosed/例外 は `cancelled=False`＝予期せぬ切断として再接続を維持**（サーバ再起動で再接続したい要件を壊さない）。`return` を `cancelled=True` に置換し finally 末尾を `if not cancelled and not _user_closing and _auto_reconnect:` に。

## 3. 検証結果
- **ユニット**: `uv run pytest -q` **165 passed**（X1の159 + reconnect 6）。私の refinement 後も green。
- **再接続テスト（実 websockets ソケット）**: `server.close()`→同一ポート再bind→`is_connected()` False→`reconnecting` 発火→再接続成功→`close()` で `_reconnect_task is None`（＝実サーバ再起動相当を実ソケットで検証）。user close は再接続しない・connect no-op・handshake 失敗で `_ws=None`・`is_connected` False・手動 reconnect、を網羅。
- **配線**: `config.ServerConfig.reconnect/reconnect_max_delay`、app が `auto_reconnect=cfg.server.reconnect` 注入、`_on_state` "reconnecting" 分岐、`client.toml` 追記、を確認。`--check` で `reconnect`含む config ロード成功。py_compile OK。境界 green。
- 既存 `test_ws_client.py`（C1/C2/X1 由来 17+ケース）は `auto_reconnect=False` 化で**挙動不変＝全 green**（再接続 OFF で従来通り、background task リーク無し）。

## 4. 後続（X2b）
- **マイク pause/resume**（C2 busy-skip を実停止に）・**全設定 GUI**（#13）＋**型 settings_store**（bool/int・演算子優先順位バグ修正）・**計測の可視化**（metrics state＋ステータス行）・**エラー/状態 UI**・**output combo 初期選択反映**。
- 接続ライフサイクルが綺麗になったので、X2b の設定即時反映（`_apply_server_settings`＋reconnect）は本 X2a の `reconnect()` を土台にできる。
- Windows 実機（あなた）: 再接続の体感確認。
