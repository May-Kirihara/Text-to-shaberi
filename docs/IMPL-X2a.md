# X2a 実装計画書 v1: 自動再接続 ＋ WSClient 接続ライフサイクル再設計

> 由来: `docs/IMPL-X2.md`（5本柱）を codex レビュー(`docs/REVIEW-codex-X2.md`)が「**WSClient の接続ライフサイクルを先に綺麗に再設計せず5本柱同時は危険**」として分割勧告。本書は **X2a＝再接続＋接続ライフサイクル**（最も risky な土台）を**単独先行**で実装する集中版。残り（mic pause/全設定GUI＋型store/計測/エラーUI）は後続 **X2b**。
> フロー: 本書 → codexレビューは X2 一括で実施済み（`docs/REVIEW-codex-X2.md` の critical 1-4＋should#10＋nit#13,14,15 を本書に反映） → GLM委託 → 私が検証/統合 → `docs/CODE-REVIEW-X2a.md`。**この仕様の通りに実装すること。**
> 前提: X1完了（`f208772`）。**新規依存ゼロ**（`random`/`asyncio` 標準）。

## 1. 目的・スコープ
**`WSClient` の接続ライフサイクルを `asyncio.Lock` で直列化して再設計し、予期せぬ切断からの自動再接続（指数バックオフ＋ジッタ）を入れる**。socket/recv loop の多重化・close と reconnect の責務混在・未接続判定の甘さ（既存の潜在バグ）を解消する。
- スコープ内:
  - `connect`/`reconnect`/`close` の **`asyncio.Lock` 直列化**＋**多重化防止**（critical#1）。
  - **`close()`（user close）と内部 `_teardown_ws()` の分離**（critical#2）。
  - **`_recv_loop` finally で self-task クリア＋再接続スケジュール**（critical#3）。
  - **`is_connected()` 導入**と speak/utter/送信失敗時の扱い（critical#4）。
  - **自動再接続ループ**（指数バックオフ＋ジッタ・config 化・注入で OFF）。`on_state("reconnecting", attempt)`。
  - `client/config.py` `ServerConfig` に `reconnect`/`reconnect_max_delay` 追加（should#10）。
  - 既存 WSClient テストを `auto_reconnect=False` 化（nit#13）。task 生成を `self._loop.create_task` に統一（nit#14）。`on_state` payload 仕様の明確化（nit#15）。
- スコープ外（X2b 以降）:
  - マイク pause/resume・全設定 GUI・型 settings_store・計測の可視化・エラー UI のステータス行。
  - UI 側の「再接続中…」表示は **最小**（`set_connection` に文字列を出すだけ）。本格ステータス行は X2b。

## 2. 既存資産（X1 時点の `ws_client.py`）
- `__init__(address, token, *, on_log, on_state, device_provider, sink_factory, loop, default_profile, stt_format, codec_pref="opus", tls_insecure=False, tls_cafile="", token_in_query=False)`。状態: `_ws`/`_recv_task`/`_active_id`/`_playback_id`/`_stt_auto_tts`/`_sink`/`_ready`/`_t_send`/`_first_chunk_logged`/`_cancelling`/`_profiles`/`_send_lock`/`_auto_tts_warned`/`_playback_codec`/`_opus_decoder`。
- `connect()`: `_ws=await websockets.connect(url, ...)` → hello → ready 受領で `_recv_task=asyncio.ensure_future(self._recv_loop())`、True。**`_ws`/`_recv_task` を無条件生成（多重化の温床）**。
- `reconnect(address,token)`: `await self.close()` → 再設定 → `connect()`。
- `close()`: `_recv_task` cancel → `_sink` abort+join → `_ws` close。
- `speak`/`utter`: `if self._ws is None: connect()`（**未接続判定が `_ws is None` のみ**）。
- `_recv_loop`: `async for raw in self._ws:` → bytes/str dispatch。`ConnectionClosed`/例外で `on_state("disconnected")`＋active 中断（`_sink.abort()`＋`_finish_stream()`）。`CancelledError` で握りつぶし。
- `on_state` イベント: ready/connected/busy/idle/disconnected/error/transcript。
- `app.py _on_state`: ready→接続✓/profiles/mic_enabled、busy→set_busy(True)/(X2bでpause)、idle→set_busy(False)、connected→log、disconnected→接続✗/set_busy(False)、error→log。
- `_build_url`/`_build_ssl_context`/`_client_codecs`（X1）。

## 3. 設計（`ws_client.py` 接続ライフサイクル再設計）

### 3.1 状態の追加
- `self._conn_lock = asyncio.Lock()`（connect/reconnect/close 直列化）。
- `self._user_closing: bool = False`（公開 `close()` で True。自動再接続を止める）。
- `self._reconnect_task: asyncio.Task | None = None`。
- `self._reconnect_attempt: int = 0`。
- `self._auto_reconnect: bool`（`__init__` 引数 `auto_reconnect: bool = True`・テストで False 注入）。
- `self._reconnect_base: float`（`__init__` 引数 `reconnect_base: float = 0.5`）。
- `self._reconnect_max: float`（`__init__` 引数 `reconnect_max_delay: float = 30.0`）。
- task 生成は **`self._loop.create_task(...)`**（`_loop` 必須前提・app は qasync loop を渡す。`_loop` 無しなら `asyncio.get_event_loop().create_task`）。`asyncio.ensure_future(loop=...)` 混在をやめる（nit#14）。

### 3.2 `connect()` の多重化防止（critical#1）
```python
async def connect(self) -> bool:
    async with self._conn_lock:
        if self.is_connected():
            return True                      # 既に接続済み＆recv_task alive → no-op
        try:
            url = self._build_url()
            ssl_ctx = self._build_ssl_context()
            kwargs = {...}                    # 既存 additional_headers(Bearer)/ssl
            self._ws = await websockets.connect(url, **kwargs)
            await self._send_hello()          # Hello 送信（_ws.send 直接、_send_lock 経由）
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            msg = parse_message(json.loads(raw))
            if not isinstance(msg, Ready):
                await self._safe_close_ws()   # 作成済み _ws を必ず close/None
                return False
            self._ready = msg; self._profiles = msg.profiles
            self._state("ready", msg); self._state("connected")
            self._recv_task = self._loop.create_task(self._recv_loop())
            return True
        except Exception as exc:
            self._log(f"接続失敗: {exc}")
            await self._safe_close_ws()
            self._state("error", str(exc))
            return False
```
- `_safe_close_ws()`: `try: await self._ws.close() except: pass; self._ws=None`（handshake 失敗時に `_ws` を残さない）。
- `connect()` は `_conn_lock` 内で走り、自動再接続ループ・手動 reconnect・speak/utter 自動接続が**直列化**される。

### 3.3 `is_connected()`（critical#4）
```python
def is_connected(self) -> bool:
    return (
        self._ws is not None
        and not getattr(self._ws, "close_code", None)        # 切断済みでない（websockets 互換）
        and self._recv_task is not None and not self._recv_task.done()
        and self._ready is not None
    )
```
- `_ws.close_code` は websockets で接続中 None、切断後コード。これで「`_ws` 残存だが切断済み」を弾く。
- speak/utter の未接続判定を `if not self.is_connected(): ok = await self.connect()` に変更。

### 3.4 `_teardown_ws()` と `close()`/`reconnect()` 分離（critical#2）
```python
async def _teardown_ws(self) -> None:
    """recv_task と sink と _ws を畳む（再接続では再利用するので _user_closing は触らない）。"""
    if self._recv_task is not None:
        self._recv_task.cancel()
        try: await self._recv_task
        except (asyncio.CancelledError, Exception): pass
        self._recv_task = None
    if self._sink is not None:
        self._sink.abort()
        await asyncio.to_thread(self._sink.join)
        self._sink = None
    await self._safe_close_ws()
    self._ready = None

async def close(self) -> None:            # 公開: user close。再接続を止める。
    self._user_closing = True
    self._cancel_reconnect_task()
    async with self._conn_lock:
        await self._teardown_ws()

async def reconnect(self, address=None, token=None) -> bool:   # 設定保存時など
    self._cancel_reconnect_task()
    if address is not None: self._address = address
    if token is not None: self._token = token
    async with self._conn_lock:
        await self._teardown_ws()
    return await self.connect()           # connect は自前で lock を取る（reconnect は手放してから呼ぶ）
```
- `_cancel_reconnect_task()`: `if self._reconnect_task and not done: cancel`、`self._reconnect_task=None`、`self._reconnect_attempt=0`。
- 注意: `reconnect()` は teardown を lock 内、その後 `connect()`（こちらも lock を取る）を **lock 外**で呼ぶ（再入回避。`asyncio.Lock` は非再入）。

### 3.5 自動再接続（critical#3・指数バックオフ）
- **`_recv_loop` finally**:
  ```python
  async def _recv_loop(self) -> None:
      try:
          async for raw in self._ws: ...   # 既存 dispatch
      except asyncio.CancelledError:
          return                            # teardown 由来。再接続しない。
      except (websockets.ConnectionClosed, Exception) as exc:
          self._log(...); 
      finally:
          if self._recv_task is asyncio.current_task():
              self._recv_task = None        # self-task をクリア（critical#3）
          # active 中断
          if self._active_id is not None:
              if self._sink is not None: self._sink.abort()
              self._finish_stream()
          self._state("disconnected")
          if not self._user_closing and self._auto_reconnect:
              self._loop.call_soon(self._schedule_reconnect)
  ```
  - **`CancelledError`（teardown/close 由来）は再接続しない**（finally の `_user_closing`/cancel 経路で抑止）。`close()`/`reconnect()` は `_teardown_ws` で recv_task を cancel → Cancelled → finally で `_user_closing`(close時) or `_cancel_reconnect_task`済(reconnect時) により再接続スケジュールされない。
  - 予期せぬ切断（ConnectionClosed）のみ `_schedule_reconnect`。
- **`_schedule_reconnect()`**（同期・`call_soon` から）: `if self._user_closing or not self._auto_reconnect: return`。`if self._reconnect_task and not done: return`。`self._reconnect_task = self._loop.create_task(self._reconnect_loop())`。
- **`_reconnect_loop()`**:
  ```python
  while not self._user_closing:
      delay = min(self._reconnect_base * (2 ** self._reconnect_attempt), self._reconnect_max)
      delay += random.uniform(0, self._reconnect_base)        # jitter
      self._reconnect_attempt += 1
      self._state("reconnecting", self._reconnect_attempt)
      await asyncio.sleep(delay)
      if self._user_closing: return
      ok = await self.connect()                                # lock 直列化
      if ok:
          self._reconnect_attempt = 0
          self._reconnect_task = None
          return
  self._reconnect_task = None
  ```
- **多重起動防止**: `_schedule_reconnect` の「既存 task 生存なら return」と、`connect()` 内 `is_connected()` no-op で、ループ中に手動操作が来ても socket は1本に収束。

### 3.6 送信失敗時の扱い（critical#4）
- `speak`/`utter` の送信 `try/except` で失敗したら従来の `_finish_stream()`＋`_state("error")` に加え、**`is_connected()` が False なら `_schedule_reconnect()`**（バックグラウンド再接続を促す）。送信失敗＝切断の可能性。
- `_send`/`_ws_send` 自体は不変（`_send_lock` 直列化）。

### 3.7 `on_state` payload 仕様（nit#15・X2b の UI が表示するだけにする土台）
| event | payload | 意味 |
|---|---|---|
| `ready` | `Ready` | ハンドシェイク完了（codec/limits/profiles） |
| `connected` | なし | 接続確立 |
| `busy` | なし | ストリーム開始 |
| `idle` | なし | ストリーム終了/中断 |
| `disconnected` | なし | 切断（直後に reconnecting が続き得る） |
| `reconnecting` | `int`(attempt) | 再接続試行中（n回目） |
| `error` | `str`(message) | エラー |
| `transcript` | `str`(text) | STT 結果（C2） |
- 既存 app `_on_state` に `"reconnecting"` 分岐を追加（最小: `window.set_connection(f"再接続中…({attempt})")`）。本格ステータス行は X2b。

### 3.8 app/config の最小変更
- `client/config.py`: `ServerConfig` に `reconnect: bool = True`・`reconnect_max_delay: float = 30.0` 追加（should#10）。`load_client_config` は `_only` 経由なので追加だけで反映。
- `client.toml`: `[server]` に `reconnect = true`・`reconnect_max_delay = 30` 追記（コメント付き）。
- `client/app.py`: `WSClient(..., auto_reconnect=cfg.server.reconnect, reconnect_max_delay=cfg.server.reconnect_max_delay)`。`_on_state` に `"reconnecting"` 分岐追加。`_shutdown` は既存（`ws.close()` が `_user_closing` を立てる）。

## 4. 受け入れ基準
- [ ] **自動再接続ユニット**（fake WS サーバ・`asyncio.run`・短 base/max 注入）: サーバが ready 後に切断すると、client が `disconnected`→`reconnecting`(n)→サーバ復帰で再 `connect`→`ready`/`connected`。再接続後に speak/utter が成立。
- [ ] **user close は再接続しない**: `await ws.close()` 後は `reconnecting` が出ず、background task が残らない（`_reconnect_task is None`）。
- [ ] **多重化防止**: 再接続ループ作動中に `connect()`/`reconnect()` を重ねても `_ws`/`_recv_task` は1本（`is_connected()` no-op）。handshake 失敗（ready 来ない）で `_ws` が None に畳まれる。
- [ ] **`is_connected()`**: 切断済み（`_ws` 残・recv_task done）で False、正常時 True。speak/utter が `is_connected()` で再接続を試みる。
- [ ] **既存テスト（auto_reconnect=False 化）**: `tests/test_ws_client.py` の全ケースが green（既存挙動不変＝再接続 OFF で従来通り）。C2/X1 の ws_client テストも green。
- [ ] **境界/コンパイル**: AST 境界・py_compile・全 pytest green。`random` 標準ライブラリ。
- [ ] **統合（手動・私がLinuxで）**: サーバ起動→GUI→**サーバを kill→再起動**で、GUI が「再接続中…」表示の後に自動で「接続✓」へ復帰。復帰後にテキスト送信が通る。GUI 終了で再接続が止まる（プロセスが残らない）。

## 5. 変更/新規ファイル
- `client/ws_client.py`（`_conn_lock`/`_user_closing`/`_reconnect_*`/`_auto_reconnect`・`connect` no-op＋`_safe_close_ws`・`is_connected`・`_teardown_ws`・`close`/`reconnect` 分離・`_recv_loop` finally 再設計・`_schedule_reconnect`/`_reconnect_loop`・送信失敗で reconnect schedule・`create_task` 統一・`auto_reconnect`/`reconnect_base`/`reconnect_max_delay` 引数）
- `client/config.py`（`ServerConfig`+reconnect/reconnect_max_delay）
- `client.toml`（`[server]` reconnect/reconnect_max_delay）
- `client/app.py`（WSClient へ reconnect 設定注入・`_on_state` に "reconnecting" 分岐）
- `tests/test_ws_client.py`（既存生成を `auto_reconnect=False` に一括更新）
- `tests/test_ws_reconnect.py`（**新規**: 再接続/ user close/多重化/is_connected）

## 6. テスト/検証
- **ユニット（必須・GPT-SoVITS/実マイク不要）**: §4。`websockets.serve` の fake サーバを途中で閉じる/再開する。`auto_reconnect=True`＋`reconnect_base=0.01`/`reconnect_max_delay=0.05` を注入して高速化。jitter は `random` を monkeypatch するか許容（小さいので待ち時間内）。`asyncio.run` 完結。各テスト終了時に `await ws.close()` で task を確実に止める（リーク防止）。
- 既存 `tests/test_ws_client.py` は **WSClient 生成箇所に `auto_reconnect=False` を一括追加**（17ケース）。挙動不変を確認。
- **統合（手動）**: §4。Linux は私、Windows はあなた（X2b と併せて）。
- AST境界・py_compile・全 pytest green 維持。**新規依存ゼロ**。

## 7. リスク・留意点
- **`asyncio.Lock` 非再入**: `reconnect()` は lock 内で `_teardown_ws` し、`connect()`（lock を自前取得）は **lock 外**で呼ぶ。`close()` も lock 内 teardown のみ。`connect()`/`reconnect()`/`close()` を互いの lock 内から呼ばない。
- **CancelledError の意味分け**: `_recv_loop` の Cancelled は **teardown/close 由来＝再接続しない**。予期せぬ切断は `ConnectionClosed`/その他例外＝再接続する。finally で `_user_closing`/`_auto_reconnect` を見て分岐。
- **self-task クリア（critical#3）**: `_recv_loop` finally で `if self._recv_task is current_task(): self._recv_task=None`。再接続スケジュールは `call_soon` で finally の外（次ループ）に送り、`_schedule_reconnect` 実行時には recv_task=None になっている。
- **既存テストのリーク（nit#13）**: 既定 `auto_reconnect=True` のままだと、fake サーバ終了後に再接続 task が走り続けテストが汚れる。**既存テストは `auto_reconnect=False`**。再接続専用テストのみ短 delay＋明示 `close()`。
- **qasync loop 前提**: task 生成は `self._loop.create_task`。`_loop` は app が qasync loop を渡す（既存）。`_loop` 無しのユニットは `asyncio.get_event_loop()` 経由でも可（`asyncio.run` 内なら現行ループ）。
- **速い切断連打**: バックオフで指数的に間隔が開く＋`is_connected()` no-op で収束。`reconnect_max_delay` で上限。
- **後方互換が最重要**: `connect`/`speak`/`utter`/`cancel`/`close`/`reconnect` の**成功時の外部挙動は不変**。自動再接続は **新規・既定ON だが既存テストは OFF 注入**で従来通り。X1/C2/S2/C1 の全テスト green を必須条件。
