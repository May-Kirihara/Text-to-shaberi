# C1 実装計画書 v2: クライアント再生 over WebSocket

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2) §4。対になるサーバは S1(`docs/IMPL-S1.md`)で実装済み（`speak`→`audio_begin`+binary(PCM)+`audio_end`、`cancel`→`stopped`、busy=side-band error）。
> フロー: 本書 → codexレビュー → GLM委託 → 検証。**この仕様の通りに実装すること。**
> **v2: codexレビュー(`docs/REVIEW-codex-C1.md`)反映** = ①end/abortは非ブロッキング(joinしない)・②cancel中のbinary破棄/audio_endはend()しない・③workerログはloop.call_soon_threadsafeでmainへ・④StreamingPlayerにthreading.Lockとrequest_abort()・⑤close()はpreroll→tail順・⑥abortとclose(drain)を用語分離・⑦queueは有界(maxsize)・⑧tokenはurllibでエンコード・⑨speak送信失敗で巻き戻し・⑩TOMLエスケープ強化+tomllib検証・⑪section判定厳密化・⑫stream_factoryはOutputStream互換kwargs・⑬スレッド系テスト追加。
> 前提: S1完了（`88877ed`）。GPT-SoVITS api_v2 が :9880 稼働、`text-to-shaberi-server` が :8765 で起動可能。

## 1. 目的・スコープ
クライアント GUI から **WSで `speak`(text) → 受信した PCM 音声フレームを逐次再生** し、**停止**で即中断する縦の疎通を完成させる。あわせて **「先頭欠け」バグ修正** と **最小の歯車設定ダイアログ** を入れる。

- スコープ内:
  - `client/ws_client.py` 本実装（`hello`/`ready`、`speak`/`cancel` 送信、`audio_begin`/binary/`audio_end`/`stopped`/`error` 受信・ディスパッチ、単一アクティブストリーム、token認証）。
  - 受信(asyncio)→再生(別スレッド)の橋渡し `client/audio/sink.py`（**新規** `PlaybackSink`）。イベントループを再生でブロックしない。
  - **`StreamingPlayer` 先頭欠け修正**＝**遅延スタート（`__init__` で `start()` しない／最初の preroll flush 時に `start()`）** ＋ preroll 既定を 40→80ms ＋ テスト用 factory 注入 seam。
  - **停止＝local abort 即時 ＋ `cancel` 送信**（interrupt SLO ≤150ms）。
  - **歯車設定ダイアログ（最小版）** `client/ui/settings_dialog.py`（**新規**）: サーバWSアドレス／トークン／プロファイル → `client.toml` 保存（コメント保持）→ 再接続。
  - `client/settings_store.py`（**新規**）: `client.toml` の scalar 文字列キーをコメント保持で in-place 更新（原子的書き込み）。
  - `client/app.py` 配線（startup接続、`on_speak`/`on_stop`/`on_settings`、再接続）、`client/ui/main_window.py`（歯車ボタン＋接続状態表示）。
  - TTFA（send→first-chunk）計測ログ。ユニットテスト3本。
- スコープ外:
  - マイク/VAD（C2）。STT/`stt_*`/`transcript`（S2）。Opus・wss/TLS（X1）。
  - 再接続バックオフ・全設定項目・デバイスホットスワップ（X2）。
  - **TTS_Virtual ルーティング配線**（`audio/routing.py` は既存だが C1 では結線しない。combo の `__ttsvirt__` は当面 `device=None` 扱いとし、ログで「ルーティングは後続」と明示）。Windows では列挙済みの実デバイス（VB-CABLE 等）を combo から選択して使う。

## 2. 既存資産（R0/M2成果）
- `client/ws_client.py`: `class WSClient: pass`（**本実装で置換**）。
- `client/audio/player.py`: `StreamingPlayer`（`__init__` で `OutputStream.start()` → **先頭欠けの原因**。本書で修正）。`play()`/`stop()` はブロッキング再生（C1では未使用、残置）。
- `client/audio/devices.py`: `list_output_devices()`・`preferred_default_output()->int|None`・`resolve_device(name|index|'default', kind)->int|None`。
- `client/audio/routing.py`: `move_app_to_sink(sink, app, pid)`（C1では未結線）。
- `client/ui/main_window.py`: `MainWindow(config, output_devices, on_speak)`・`set_on_speak/set_on_stop`・`selected_output_device()->None|'__ttsvirt__'|int`・`set_busy(bool)`・`log(str)`。
- `client/config.py`: `ClientConfig`(server.address/server.token, output, mic, ui, profile.active_id)・`load_client_config(path)`。
- `client/app.py`: `run_gui(path)`（qasync ループ構築済み・`on_speak`/`on_stop` は stub「WS未接続（C1で実装）」）。`run_check`。`DEFAULT_CONFIG=parents[3]/"client.toml"`。
- `common/protocol.py`: `Hello/Ready/Speak/AudioBegin/AudioEnd/Cancel/Stopped/ErrorMsg/AudioFormatPayload/ProfileInfo`・`parse_message(dict)`。
- `common/pcm.py`: `_pcm_to_float32`・`bytes_per_sample`・`split_full_samples`。
- 依存（pyproject `client` extra に既存）: `websockets`・`qasync`・`PySide6`・`sounddevice`・`soxr`・`numpy`。**新規依存は追加しない。**

## 3. 設計

### 3.1 スレッド/イベントループ・モデル（最重要）
qasync で **Qt と asyncio が同一スレッド・同一ループ**で回る（`run_gui` で構築済み）。
- WS送受信は **asyncio タスク**（`WSClient._recv_loop`）。
- 再生（`sounddevice` のブロッキング `write`）は **専用ワーカースレッド**（`PlaybackSink`）。受信側は `queue` への **非ブロッキング put** だけ行い、**イベントループを絶対にブロックしない**。
- Qt の signal callback（`on_speak`/`on_stop`/`on_settings`）は同期関数。中で `asyncio.ensure_future(...)` してコルーチンを現在のループに載せる。

### 3.2 `client/audio/player.py`: `StreamingPlayer` 修正（先頭欠け・スレッド安全）
**原因**: `__init__` で `OutputStream.start()` を呼ぶと、最初の preroll が貯まるまでデバイスは空のリングバッファを再生（無音/underflow）し、最初の音が欠ける。
**修正方針**: **遅延スタート**（最初に実データを `write` する直前に `start()`）＋ **`threading.Lock` でスレッド安全化**（codex#4）。
**スレッド役割分担（重要・codex#4,#6）**:
- `write_bytes` / `close()`（drain を伴う**正常終了**）は **worker スレッド所有**。
- `request_abort()`（**即停止**・別スレッドから安全）は **main スレッドが呼ぶ**。stream を abort するだけで `close()`（drain）は呼ばない。デバイス解放（`stream.close()`）は worker が `close()` で行う。
- `self._lock = threading.Lock()`、`self._aborted = False`、`self._closed = False`、`self._started_stream = False`。`_aborted`/`_closed`/stream 操作はすべて lock 内。

実装:
- `__init__`: stream を**生成のみ**（`start()` しない）。`preroll_ms` 既定 **80**。
- `_ensure_started()`（lock 前提で呼ぶ）: `if not self._started_stream: self._stream.start(); self._started_stream = True`。
- `write_bytes(data)`: 先頭で `with self._lock: if self._aborted or self._closed: return`。preroll が満ちて combined を書く直前に `_ensure_started()` → write。live 分岐（`_started_audio` True）は既に started。stream 例外時は `request_abort()` 相当にして raise しない（worker ループが break）。
- `request_abort()`（**main から**・idempotent）: `with self._lock: if self._aborted: return; self._aborted = True; if self._started_stream: self._stream.abort()`。**`close()` は呼ばない**（worker が _ABORT 受信時に `close()` でデバイス解放）。
- `close()`（**worker から**・idempotent）: `with self._lock:` `if self._closed: return; self._closed = True`。
  - `_aborted` のとき: **drain も sleep もしない**。started なら `stream.stop()`、常に `stream.close()`（guard）。
  - 正常終了のとき: 書くべき残データ（**順序: `preroll` → resampler `last=True` tail**・codex#5）を連結。あれば `_ensure_started()` してから write。started なら `time.sleep(latency+0.05)` → `stop()` → `close()`。一度も started でなければ sleep/stop 省略、`stream.close()` のみ。
- **テスト seam（注入・codex#12）**: シグネチャを下記に拡張。注入時は `sounddevice`/`soxr` を一切 import しない。
  ```python
  def __init__(self, src_rate, channels=1, device=None, encoding="pcm_s16le",
               preroll_ms=80, *, stream_factory=None, resampler_factory=None,
               target_sr=None):
  ```
  - `target_sr` 未指定: 従来通り `device`(None→`preferred_default_output()`) を解決し `_device_samplerate(sd, device)`。指定時はそれを使う。
  - `stream_factory` 未指定: `import sounddevice as sd` → `sd.OutputStream(samplerate=target_sr, channels=channels, dtype="float32", device=device, latency="low")`。**指定時は同じ kwargs で呼ぶ**: `stream_factory(samplerate=target_sr, channels=channels, dtype="float32", device=device, latency="low")`（fake が kwargs を検証可能・codex#12）。
  - `resampler_factory`: `target_sr != src_rate` のときのみ生成。未指定なら `import soxr; soxr.ResampleStream(...)`。指定時は `resampler_factory(src_rate, target_sr, channels)`。
- **挙動不変条件**: `target_sr == src_rate`（リサンプル不要）でも遅延スタートは同じ。resampler 無しでも preroll→`_ensure_started`→write の順は守る。tail は resampler 有りのときのみ存在。

### 3.3 `client/audio/sink.py`: `PlaybackSink`（新規）
受信(asyncio)→再生(thread) の橋渡し。**1発話=1 sink ライフサイクル**（begin→…→end / abort）。
**非ブロッキング契約（最重要・codex#1）**: `feed`/`end`/`abort` は **sentinel 投入のみで即 return**。`_recv_loop` や Qt callback から**同期 `join()` しない**。worker は自スレッドで drain/close して終了する（daemon）。最終 `close()`（WSClient終了時）でのみ `await asyncio.to_thread(sink.join)` で待つ。
```python
class PlaybackSink:
    def __init__(self, *, player_factory=StreamingPlayer, on_log=None, loop=None): ...
    def begin(self, fmt: AudioFormatPayload, device): ...   # ワーカースレッド起動。playerはワーカー内で生成
    def feed(self, chunk: bytes) -> bool: ...               # put_nowait。満杯ならFalse（呼び出し側がoverload処理）
    def end(self) -> None: ...                              # _END sentinel 投入のみ（即return、join しない）
    def abort(self) -> None: ...                            # player.request_abort()即時 + _ABORT sentinel（即return）
    def join(self, timeout: float = 2.0) -> None: ...       # asyncio.to_thread 経由でのみ呼ぶ
```
- `begin`: `self._q = queue.Queue(maxsize=256)`（**有界**・codex#7。1chunk≤65536B→最悪~16MB上限）。`threading.Thread(target=self._run, args=(fmt, device), daemon=True)` を start。
- `_run(fmt, device)`: ワーカー内で `player = player_factory(src_rate=fmt.sample_rate, channels=fmt.channels, device=device, encoding=fmt.codec)`。**生成後に `self._player = player`**（abort から触れる）。生成失敗は `self._log(...)` で報告し終了。ループ:
  ```
  while True:
      item = self._q.get()
      if item is _END:   player.close();  break    # 正常 drain+close
      if item is _ABORT: player.close();  break    # request_abort 済み→close でデバイス解放(drainなし)
      player.write_bytes(item)                      # write内で例外は握り潰し abort 状態へ
  ```
  - write 中に `_aborted` になった場合 `write_bytes` は即 return（§3.2）。worker は次に _ABORT を受けて break。
- `feed(chunk)`: `try: self._q.put_nowait(chunk); return True except queue.Full: return False`（**非ブロッキング**）。満杯=再生が追いつかない→呼び出し側(WSClient)が abort+cancel+log（codex#7。本格バックプレッシャはC2）。
- `_log(msg)`: **worker から Qt/ログへ直接触らない（codex#3）**。`if self._loop is not None: self._loop.call_soon_threadsafe(self._on_log, msg) else self._on_log(msg)`。
- `end()`: `self._q.put_nowait(_END)`（満杯フォールバックは drain 不要なので put でも可だが簡潔に put_nowait→Full時は _ABORT 投入）。**join しない**。
- `abort()`: idempotent（`self._aborting` ガード）。`if self._player: self._player.request_abort()`（main から即停止・PortAudio Pa_AbortStream は別スレッド安全）。`self._q.put_nowait(_ABORT)`（満杯なら queue を drain して投入）。**join しない**。
- `join(timeout)`: `self._thread.join(timeout)`。WSClient の最終 `close()` から `await asyncio.to_thread(...)` 経由でのみ呼ぶ。
- `self._player` は生成後代入・未生成は None ガード。`self._loop` は WSClient/app から渡す（asyncio ループ）。

### 3.4 `client/ws_client.py`: `WSClient`（新規・本実装）
```python
class WSClient:
    def __init__(self, address: str, token: str = "", *,
                 on_log=None, on_state=None, device_provider=None,
                 sink_factory=PlaybackSink): ...
    async def connect(self) -> bool: ...        # 接続→hello→ready。成功でTrue
    async def reconnect(self, address=None, token=None) -> bool: ...  # close→connect
    async def speak(self, text: str, profile: str | None = None) -> None: ...
    async def cancel(self) -> None: ...
    async def close(self) -> None: ...
```
- 状態: `_ws`・`_recv_task`・`_active_id: str|None`・`_sink: PlaybackSink|None`・`_ready: Ready|None`・`_t_send: float|None`・`_first_chunk_logged: bool`・`_cancelling: bool`・`_loop`（asyncio ループ参照）。
- `on_state(event, payload)`: `"ready"`(payload=Ready)／`"busy"`／`"idle"`／`"connected"`／`"disconnected"`／`"error"`(payload=message)。app/MainWindow がUI反映に使う。
- **接続/認証（codex#8）**: `address` に token があれば **`urllib.parse` でクエリ付与**: `urlsplit` → `parse_qsl(query)` に `("token", token)` 追加 → `urlencode` → `urlunsplit`。文字列連結はしない（token に `&`/`#`/空白が含まれても安全）。`websockets.connect(url)`。失敗は `on_state("error", ...)` + False。
- `connect`: `_ws=await websockets.connect(url)` → `await _send(Hello(client_name="text-to-shaberi", client_id="", caps={"codecs":["pcm_s16le"]}))` → 最初の受信が `ready` でなければ error扱い・close。`_ready=ready`、`on_state("ready", ready)`・`on_state("connected")`。`_recv_task=asyncio.ensure_future(self._recv_loop())`。
- `_recv_loop`: `async for raw in self._ws:`（`websockets` は str/bytes を yield）
  - bytes → `_on_binary(raw)`
  - str → `m=parse_message(json.loads(raw))` し type 分岐（不正は `on_log` のみで継続）
  - `ConnectionClosed`/例外 → `on_state("disconnected")`、active あれば `_sink.abort()`・`_finish_stream()`。
- ディスパッチ（**id 照合規則は S1 と対。cancel 中の状態規則を明文化・codex#2**）:
  - `ready`（再受信は無視/上書き）。
  - `audio_begin`: `id==_active_id and not _cancelling` のときのみ `_sink=sink_factory(loop=self._loop, on_log=...)`; `fmt=msg.format`（無ければ `_ready.tts`）; `device=self._resolve_device()`; `_sink.begin(fmt, device)`。`id≠active` は side-band → ログのみ。
  - binary: `_active_id and _sink and not _cancelling` のとき `ok=_sink.feed(raw)`。`ok is False`（満杯=overload・codex#7）→ `_sink.abort()` + `await _send(Cancel(id=_active_id))` + `_cancelling=True` + ログ「再生が追いつきません」。**`_cancelling` 中の binary は破棄（feed しない）**。初回 feed 成功時に **TTFA ログ**（`(loop.time()-_t_send)*1000` ms、once）。
  - `audio_end`: `id==active` → **`_cancelling` なら `_sink.end()` を呼ばず**（既に abort 済み・再 drain 防止）`_finish_stream()` のみ。通常は `_sink.end()` → `_finish_stream()`（総量/TTFA ログ）。
  - `stopped`: `id==active` → cancel の正規終端。sink は cancel 時に abort 済み。`_finish_stream()`・ログ "stopped"。
  - `error`: `id==active` → **terminal**: `_cancelling` なら既に abort 済みなので `_finish_stream()`（idle 扱い・ログ）。非 cancel なら `_sink.abort()`・`_finish_stream()`・`on_state("error", msg.message)`。`id≠active`/`id=None` → **side-band**: ログのみ（再生継続）。
- `_finish_stream()`: `_sink=None`・`_active_id=None`・`_cancelling=False`・`_first_chunk_logged=False`・`on_state("idle")`。**abort はしない**（呼び出し側が必要時に実施済み）。
- `speak`: 未接続なら `await self.connect()`、失敗で `on_log("接続できません")` し return（busy にしない）。`_active_id` 在中（busy）なら `on_log("再生中です")` し return（ローカルガード。サーバも side-band busy を返す）。`id=uuid4().hex`・`_active_id=id`・`_t_send=loop.time()`・`_first_chunk_logged=False`・`_cancelling=False`・`on_state("busy")`。**送信失敗の巻き戻し（codex#9）**: `try: await _send(Speak(id=id, text=text, profile=profile or cfg値 or None)) except Exception as e: _finish_stream(); on_state("error", str(e))`（active/busy を残さない）。
- `cancel`: `_active_id` 在中なら **即** `_sink.abort()`（snappy・ローカル中断）・`_cancelling=True`・`try: await _send(Cancel(id=_active_id)) except Exception: pass`。active 無しは no-op（ログのみ）。`_active_id` は `stopped`/`error`/`audio_end` 受信まで保持（遅延フレームの帰属判定のため。`_cancelling` 中の binary/audio_end は上記規則で安全に処理）。
- `_send(model)`: `await self._ws.send(model.model_dump_json())`。`_resolve_device()`: `device_provider()` の戻り（None/'__ttsvirt__'/index）を `resolve_device` で index へ。`'__ttsvirt__'` は当面 None＋ログ。
- `close`: `_recv_task` キャンセル、`_sink` あれば `abort()` してから `await asyncio.to_thread(self._sink.join)`、`_ws` close。

### 3.5 `client/settings_store.py`: `client.toml` 更新（新規）
```python
def update_client_toml(path, updates: dict[str, dict[str, str]]) -> None:
```
- `updates` 例: `{"server": {"address": "...", "token": "..."}, "profile": {"active_id": "..."}}`。**scalar 文字列キーのみ**対象。
- 行指向で **コメント保持**: 既存行（`[section]` 配下に `key =`）は値だけ置換（行頭インデント保持、その行のインライン注釈は捨ててよい）。section/key が無ければ末尾に `[section]` と `key = "value"` を追記。
- **section 判定の厳密化（codex#11）**: 対象は **完全一致の通常 table header `[section]` のみ**（行を strip して `[name]` に一致）。`[server.auth]`・`[[server]]`（array of tables）・コメント `# ...` は対象外/誤認しない。section の範囲は「その header の次行から、次の任意 table header（`[...]`/`[[...]]`）の直前まで」。コメント行・空行は key として扱わない（`^\s*#` で除外）。
- **エスケープ（codex#10）**: `_toml_str(v)` は TOML basic string として `\`→`\\`、`"`→`\"`、改行 `\n`/復帰 `\r`/タブ `\t` をエスケープ、その他 U+0000–U+001F 制御文字は `\u00XX`。
- **検証付き原子的書き込み（codex#10）**: 生成した全文を **`tomllib.loads()` でパース検証**してから、同ディレクトリの一時ファイルへ書いて `os.replace(tmp, path)`。検証失敗時は書き込まず例外（呼び出し側がログ表示、既存ファイルは破壊しない）。
- tomllib/`re`/`os` のみ使用（新規依存なし）。

### 3.6 `client/ui/settings_dialog.py`: 歯車設定（新規・最小版）
- `class SettingsDialog(QDialog)`: 親 + `cfg: ClientConfig` + `profiles: list[ProfileInfo]` を受ける。
  - サーバWSアドレス（`QLineEdit`、既定 `cfg.server.address`）。
  - トークン（`QLineEdit`、`EchoMode.Password`、既定 `cfg.server.token`）。
  - プロファイル（`QComboBox`: 先頭「(サーバ既定)」=`""`、続けて `profiles` の `id`。現在値 `cfg.profile.active_id` を選択）。
  - OK/キャンセル（`QDialogButtonBox`）。
  - `values() -> dict`: `{"address":..., "token":..., "active_id":...}` を返す（呼び出し側が保存）。

### 3.7 `client/ui/main_window.py`: 歯車ボタン＋接続表示
- device_row に **「⚙」`QPushButton`** を追加（右端）。`set_on_settings(cb)` を追加し clicked で呼ぶ。
- 接続状態 `QLabel`（"接続: …"）を追加。`set_connection(text)` で更新。
- 既存 `set_busy`/`set_on_speak`/`set_on_stop`/`selected_output_device` は不変。

### 3.8 `client/app.py`: 配線
- `run_gui`:
  - qasync ループ構築後（`asyncio.set_event_loop(loop)` 済み）に `ws = WSClient(cfg.server.address, cfg.server.token, on_log=window.log, on_state=_on_state, device_provider=window.selected_output_device, loop=loop)`。WSClient は `loop` を `PlaybackSink(loop=loop, ...)` に渡し、worker ログを `call_soon_threadsafe` で main へ戻す（codex#3）。
  - `_on_state`/`_recv_loop`/`on_log` は **すべて main(qasync) スレッド上**で実行されるため Qt ウィジェット直操作で安全。worker スレッドからの経路だけ `call_soon_threadsafe` を使う。
  - `_on_state(event, payload)`: `"ready"`→`window.set_connection("接続 ✓"); _profiles=payload.profiles`、`"busy"`→`window.set_busy(True)`、`"idle"`→`window.set_busy(False)`、`"connected"`→ログ、`"disconnected"`→`window.set_connection("接続 ✗"); window.set_busy(False)`、`"error"`→`window.log("[error] "+str(payload))`。
  - `on_speak(text)`: `asyncio.ensure_future(ws.speak(text))`。
  - `on_stop()`: `asyncio.ensure_future(ws.cancel())`。
  - `on_settings()`: `SettingsDialog(window, cfg, _profiles)` を exec。OK なら `update_client_toml(config_path, {"server":{...},"profile":{...}})` → `cfg` をメモリ更新 → `asyncio.ensure_future(ws.reconnect(new_address, new_token))`。
  - startup: `window.show()` 後・`loop.run_forever()` 前に `asyncio.ensure_future(ws.connect())`（接続失敗してもGUIは起動。speak時に再接続）。
  - 終了時 `ws.close()` を best-effort（loop終了フックは最小で可）。
- `run_check` は不変。

### 3.9 フォーマット/コーデック
C1 は **`pcm_s16le` 固定**。再生フォーマットは `audio_begin.format`（GPT-SoVITS 既定 32000/mono）を採用、無ければ `ready.tts`。出力デバイス rate へ `StreamingPlayer` 内 soxr 変換。

## 4. 受け入れ基準
- [ ] **先頭欠け修正**: `StreamingPlayer.__init__` で `start()` を**呼ばない**。最初の `write`（preroll flush）直前に `start()` が呼ばれる（ユニットで呼び出し順を検証）。短い発話（preroll 未満で `close()`）でも `start()`→残データ(**preroll→tail 順**)write→close される。一度も write しない `request_abort()`→`close()` で `start()` 無しでもクラッシュしない。`request_abort()`/`close()` は idempotent（Lock）。
- [ ] **WSClient ユニット**: fake WS サーバ（`websockets.serve`）に対し `hello`→`ready` を受領、`speak`→`audio_begin`→binary×N→`audio_end` を **fake sink** で `begin/feed×N/end` として受ける。`error(id≠active)` は side-band（再生継続=abort されない）。`error(id==active)` は terminal（sink.abort）。`cancel` で sink.abort 即時＋`cancel` 送信＋以降 binary 破棄＋`audio_end` で `end()` を呼ばない。token あり接続で URL に token クエリが付く。speak 送信失敗で active/busy が残らない。
- [ ] **PlaybackSink ユニット（codex#13）**: `end()`/`abort()` が **即 return（join しない）**。fake player の `write` ブロック中に `abort()` で停止できる。`abort()` 複数回が安全。worker 生成失敗/例外時のログが **`loop.call_soon_threadsafe` 経由**で main に渡る（worker から直接 on_log を呼ばない）。queue 満杯で `feed` が False を返す。
- [ ] **settings_store ユニット**: 既存キー置換でコメント/他行保持、欠損キー追記、欠損section追記。`[server.auth]`/`[[server]]`/コメント行を誤認しない。改行/制御文字/`"`/`\` を含む値が TOML として再パース可能。原子的書き込み（一時ファイル→replace）。
- [ ] イベントループ非ブロッキング: 再生は別スレッド（`PlaybackSink`）。受信ループ・Qt callback は `feed`/`end`/`abort`(即return) のみで **同期 join しない**。
- [ ] AST境界（client→server/torch/qwen/silero 禁止）・py_compile・既存pytest 全green。新規 client ファイルは sounddevice/soxr を**遅延import**（モジュールトップで import しない）。
- [ ] **統合（手動・私がLinuxで）**: `text-to-shaberi-server` 起動 → GUI 起動 → テキスト送信で **音が鳴り、先頭が欠けない**。停止で即中断。TTFA(send→first-chunk) ログ出力。歯車でアドレス/トークン保存→`client.toml` 更新・再接続。
- [ ] **Windows実機（あなた）**: WASAPI 既定出力で再生、VB-CABLE を combo 選択で配信先へ、停止・先頭欠け無しを確認。

## 5. 変更/新規ファイル
- `client/ws_client.py`（**置換**: `WSClient` 本実装）
- `client/audio/sink.py`（**新規**: `PlaybackSink`）
- `client/audio/player.py`（`StreamingPlayer` 遅延スタート＋factory注入＋preroll 80）
- `client/ui/settings_dialog.py`（**新規**: `SettingsDialog`）
- `client/settings_store.py`（**新規**: `update_client_toml`）
- `client/ui/main_window.py`（歯車ボタン・接続ラベル・`set_on_settings`/`set_connection`）
- `client/app.py`（WSClient/PlaybackSink 配線・`on_speak`/`on_stop`/`on_settings`・startup接続・再接続）
- `tests/test_player_lazy_start.py`（**新規**）
- `tests/test_playback_sink.py`（**新規**）
- `tests/test_ws_client.py`（**新規**）
- `tests/test_settings_store.py`（**新規**）

## 6. テスト/検証
- **ユニット（必須・GPT-SoVITS不要・sounddevice不要）**:
  - `test_player_lazy_start.py`: fake `OutputStream`（start/write/stop/abort/close を順序記録）と fake resampler を `stream_factory`/`resampler_factory`/`target_sr` で注入。`__init__` 後に `start` 未呼出、最初の write 前に `start`、短発話 close で **preroll→tail 順**に write→stop/close、`request_abort()`→`close()` の idempotency（複数回安全・start 無し経路でクラッシュしない）を assert。
  - `test_playback_sink.py`（codex#13）: fake player を注入し、`end()`/`abort()` が即 return（呼び出しスレッドをブロックしない）、`write` ブロック中の `abort()`、`abort()` 多重呼び出し、生成失敗/例外ログが `loop.call_soon_threadsafe` 経由（worker から直接 on_log を呼ばない）、`feed` 満杯で False、を検証。`loop` は `asyncio.new_event_loop()` を用意し `call_soon_threadsafe` の到達を確認。
  - `test_ws_client.py`: `websockets.serve` で S1 シーケンスを返す fake サーバを立て、`asyncio.run(...)` 内で `WSClient` を `sink_factory=FakeSink`（begin/feed/end/abort を記録）で動かす。ready/speak→begin+feed+end / side-band error 継続 / terminal error abort / cancel→abort+Cancel送信+以降 binary 破棄+audio_end で end() 非呼出 / token クエリ付与 / speak 送信失敗の巻き戻し を検証。（`pytest-asyncio` は使わず `asyncio.run` で完結。）
  - `test_settings_store.py`: 一時 `client.toml` に対し更新→tomllib で再読込し値一致＋コメント行残存、欠損 section/key 追記、`[server.auth]`/`[[server]]`/コメント非誤認、改行/制御文字含む値の再パース可、を assert。
- **統合（手動）**: 上記 §4。サーバ＋実 GPT-SoVITS＋実オーディオ。Linux は私、Windows はあなた。
- AST境界・py_compile・既存pytest 全green維持。

## 7. リスク・留意点
- **非ブロッキング契約（codex#1）**: `_recv_loop`/Qt callback から `PlaybackSink.join` を**絶対呼ばない**。`end()`/`abort()` は sentinel 投入で即 return。join は最終 `close()` で `await asyncio.to_thread(...)` のみ。これを破ると残再生ぶん GUI/WS が固まる。
- **cancel 中の状態規則（codex#2）**: `_cancelling` 中の binary は破棄、`audio_end(id==active)` は `end()` を呼ばず `_finish_stream()` のみ、`stopped` が正規終端、`error(id==active)` はキャンセル中なら idle 扱い。abort 済み sink を再 drain しない。
- **Qt スレッド安全（codex#3,#4）**: Qt ウィジェット操作は main(qasync) スレッドのみ。worker からのログは `loop.call_soon_threadsafe`。`StreamingPlayer` は `threading.Lock` で `_aborted`/`_closed`/stream 操作を保護。main は `request_abort()`（abort のみ）、worker が `close()`（デバイス解放）。
- **遅延スタートの drain（リグレッション防止）**: 短い発話で preroll 未達のまま `close()` する経路を必ず通す（close でも `_ensure_started` を呼び preroll を吐く）。close の書き込み順は **preroll → resampler tail**（codex#5）。さもないと「全部鳴らない/順序逆転」になる。
- **abort の用語分離（codex#6）**: `request_abort()`＝即停止（`stream.abort()` のみ、drain しない）。正常終了の `close()`（drain/sleep）と混同しない。
- **queue 有界（codex#7）**: `queue.Queue(maxsize=256)`。満杯=再生が追いつかない→ abort+cancel+log。本格バックプレッシャ（WS 側 await）は C2。
- **qasync 上の ensure_future**: `run_gui` で `asyncio.set_event_loop(loop)` 済み。signal callback から `asyncio.ensure_future` で現在ループに載る。`asyncio.get_event_loop()` ではなく構築済み `loop` 参照を使う。
- **token クエリ付与（codex#8）**: `urllib.parse`（urlsplit/parse_qsl/urlencode/urlunsplit）でエンコード。文字列連結しない。
- **client.toml 安全更新（codex#10,#11）**: 行指向 in-place（全 dict ダンプにしない・コメント保持）。table header は完全一致 `[section]` のみ対象、`[a.b]`/`[[a]]`/コメント非誤認。値は制御文字までエスケープし、書き込み前に `tomllib.loads()` で検証。原子的 `os.replace`。
- **新規依存ゼロ**: `websockets`/`PySide6`/`sounddevice`/`soxr` は client extra に既存。`pytest-asyncio`・`tomli-w` は**追加しない**。
- **境界**: 新規 client ファイルは `server`/`torch`/`qwen_asr`/`silero_vad` を import しない。`sounddevice`/`soxr` はトップレベル import しない（`--check`/CI で portaudio 不在でも落とさない）。
- S2向け: `features.auto_tts` が true 化したら `transcript`→自動TTS の `parent_id` 連鎖を C 側でも扱う（C1では `stt_*`/`transcript` 受信は無視/ログのみ）。
