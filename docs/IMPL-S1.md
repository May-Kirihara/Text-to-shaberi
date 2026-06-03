# S1 実装計画書 v2: サーバ TTS over WebSocket

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2) §4。フロー: 本書 → GLM委託 → 検証。
> **v2: codexレビュー(`docs/REVIEW-codex-S1.md`)反映** = side-band error規則／create_app注入とlifespan所有権／token拒否の観測仕様／`ws.receive()`分岐／parse・validationエラー規則／send一元化／finally解除判定／ProfileInfo.id=stem／CancelledError best-effort。
> 前提: R0完了。GPT-SoVITS api_v2 が :9880 稼働。**この仕様の通りに実装すること。**

## 1. 目的・スコープ
WSサーバ `/ws` を本実装し、**`speak`(text) → GPT-SoVITS streaming → PCM音声フレーム送信**＋`cancel`＋ストリーム状態機械。
- スコープ内: `/ws`、token認証、`hello/ready`、`speak`→`audio_begin`+binary+`audio_end`、`cancel`→`stopped`、同方向同時1ストリーム、エラー処理、TTSClientライフサイクル、サーバ単体検証。
- スコープ外: STT/`stt_*`/`transcript`（S2）、Opus（X1。S1はPCM固定）、クライアント（C1）。

## 2. 既存資産（R0成果）
- `server/ws_server.py: create_app()->FastAPI`（`/healthz`＋`/ws` stub）→ **本実装で置換**。
- `server/config.py`: `ServerConfig`(bind/stt/tts/transport)・`validate_bind`・`load_server_config`。
- `server/tts/`: `TTSClient.stream(profile,text)->AsyncIterator[bytes]`・`load_profile(profiles_dir,name)->TTSProfile`・`TTSProfile.audio`(encoding/sample_rate/channels)・`TTSProfile.name`。
- `common/protocol.py`: 各pydanticモデル・`parse_message(dict)->BaseModel`。
- `server/app.py: run_server()`（R0 stub）。

## 3. 設計

### 3.1 アプリ構成 / ライフサイクル（codex#3,#4）
- **正式シグネチャ**: `create_app(config: ServerConfig | None = None, tts_client=None) -> FastAPI`。
  - `config is None` のときのみ `load_server_config(既定パス)`（TestClient便宜用）。
  - `profiles_dir` 解決: `config.tts.profiles_dir` 空なら `Path(__file__).resolve().parents[3] / "profiles"`。`app.state.profiles_dir` に格納。
  - **available_profiles 列挙**: `profiles/tts/*.toml` を列挙し、各 `ProfileInfo(id=path.stem, name=load_profile(...).name)`（codex#5）。`app.state.available_profiles`。
  - `app.state.config` に config 保持。
- **TTSClient 所有権（codex#3）**: `app.state.tts_client`。FastAPI `lifespan` で:
  - 引数 `tts_client is None` のときのみ `TTSClient()` を生成し `owns=True`、shutdownで `await aclose()`。
  - 注入されていれば**それを使い、上書きも close もしない**（`owns=False`）。
- `server/app.py: run_server()`（codex#4）: `cfg = load_server_config(-c既定 parents[3]/"server.toml")` → `validate_bind(cfg.bind)` → `uvicorn.run(create_app(cfg), host=cfg.bind.host, port=cfg.bind.port)`。**必ず `create_app(cfg)` に渡す（create_app内で再ロードしない）**。

### 3.2 接続・認証（codex#2）
- `@app.websocket("/ws")`。**accept前にtoken検証**:
  - `config.bind.token` 空 → 受理。
  - 非空 → `ws.query_params.get("token")` or `Authorization: Bearer` と照合。不一致なら **accept せず拒否**（`await ws.close(code=1008)`。Starletteの挙動上、観測は「接続確立不可 / 1008 / 403相当のdenial」いずれか）。
- 受理後 `Session` 生成（§3.3）。

### 3.3 セッション / オーケストレータ（`server/orchestrator.py` 新規）
- `class Session`: 1接続の状態。属性: `ws`, `state`(config/profiles_dir/tts_client/available_profiles), `session_id=uuid4().hex`, `active_stream_id: str|None`, `active_task: asyncio.Task|None`, `_send_lock: asyncio.Lock`。
- **全送信は一元化（codex#11）**: `async def _send_model(self, model)`／`async def _send_bytes(self, data)` のみで送る。`model_dump_json()` は **Lock外**で作り、`async with self._send_lock:` で `ws.send_text/send_bytes`。**他所から直接 `ws.send_*` しない**。
- **受信は `ws.receive()`（codex#9）**: dict を見て `"text"` キーなら `json.loads`→`parse_message`、`"bytes"` キーなら `error(id=None,"binary unsupported in S1")`。
- **エラー規則（codex#1,#10）**:
  - `error(id == active_stream_id)` = **そのstreamの終端**。
  - `error(id != active_stream_id)` または `id=None` = **side-band**（active streamは継続）。
  - binaryは**直近の未終端 `audio_begin` の id** に属する（side-band JSONは帰属を変えない）。
- `async def run()`:
  - **handshake**: 最初の受信が `hello` 以外 / JSON不正 / 不明type → `error(id=None,...)` 後 **close**。`hello` なら `ready` を返す:
    - `codec="pcm_s16le"`、`tts=AudioFormatPayload(codec="pcm_s16le", sample_rate=active.audio.sample_rate, channels=active.audio.channels)`（active = `load_profile(profiles_dir, config.tts.active_profile)`。ロード失敗は `error`後close）、`profiles=available_profiles`、`limits={"max_chunk_bytes":65536}`、`features={"auto_tts": false}`、`server_time=time.time()`。
  - **ループ**（handshake後の parse/validation 失敗は `error(id=None,...)` で**継続**・codex#10）: `Speak`→`_handle_speak`、`Cancel`→`_handle_cancel`、`stt_*`等→`error(id=None,"unsupported in S1")`、`Speak.id` 空→`error`。
  - 切断/終了時 cleanup: `active_task` 生存なら `cancel()`（**この経路では Stopped を送らない**・codex#8）。
- `_handle_speak(msg)`:
  - **同時1ストリーム（busy は side-band・codex#1）**: `active_task` 未完了なら `error(id=msg.id, message="busy: stream already active")`（id≠activeなのでside-band）を返し**新規は受けない**。
  - `name = msg.profile or config.tts.active_profile`。`load_profile`失敗→`error(id=msg.id,...)`。
  - `active_stream_id=msg.id`、`active_task=asyncio.create_task(self._stream_tts(msg.id, profile, msg.text))`。
- `_stream_tts(stream_id, profile, text)`:
  ```
  fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=profile.audio.sample_rate, channels=profile.audio.channels)
  await self._send_model(AudioBegin(id=stream_id, format=fmt))
  try:
      async for chunk in self.state.tts_client.stream(profile, text):
          await self._send_bytes(chunk)
      await self._send_model(AudioEnd(id=stream_id))
  except asyncio.CancelledError:
      await self._send_stopped_best_effort(stream_id)   # try/except Exception 内
      raise
  except Exception as exc:
      await self._send_model(ErrorMsg(id=stream_id, message=repr(exc)))
  finally:
      task = asyncio.current_task()
      if self.active_task is task and self.active_stream_id == stream_id:
          self.active_task = None; self.active_stream_id = None
  ```
  - `_send_stopped_best_effort`: `try: await self._send_model(Stopped(id=stream_id)) except Exception: pass`（切断済みは無視・codex#6）。`CancelledError` 自体は re-raise。
- `_handle_cancel(msg)`（codex#8）: `active_stream_id==msg.id` かつ `active_task` 生存→ `active_task.cancel()`（Stoppedは `_stream_tts` が一元送信）。active無し/不一致→ `error(id=msg.id, message="no active stream")`。

### 3.4 コーデック/フォーマット
S1は **`pcm_s16le` 固定**。`audio_begin.format` は profile.audio 由来（GPT-SoVITS既定 32000/mono）。`media_type="raw", streaming_mode=3`（first-chunk ~150ms実測）。

## 4. 受け入れ基準
- [ ] `TestClient(create_app(cfg, tts_client=fake)).websocket_connect("/ws")` で `hello`→`ready`（codec/tts/profiles/`features.auto_tts==false`・codex#12）。
- [ ] `speak`→`audio_begin`→**binary PCMチャンク（Fakeは2チャンク以上 yield・codex#13）**→`audio_end` の順。bytes/(2*ch*sr) が妥当秒数。
- [ ] 再生中の2回目 `speak` は `error(id=B, "busy...")`（**side-band**・active継続、audio_endで終端していない）。
- [ ] `cancel(id)` で進行中が止まり `stopped(id)`。GPT-SoVITSへのHTTPもキャンセル。active無し/不一致 cancel は `error("no active stream")`。
- [ ] token設定時、誤tokenは `/ws` 接続不可（1008 もしくは denial/403 として観測・codex#2）。正tokenは疎通。
- [ ] handshake前の不正は `error`後close、handshake後の不正type/JSONは `error(id=None)` で継続。binary受信は `error("binary unsupported in S1")`。
- [ ] 切断で active_task キャンセル・リークなし（Stoppedは送らない）。
- [ ] AST境界テスト(server→PySide6/sounddevice禁止)・py_compile・既存pytest 全green。

## 5. 変更/新規ファイル
- `server/ws_server.py`（`create_app(config=None, tts_client=None)`・`/ws`本実装・lifespan(owns制御)・profiles列挙）
- `server/orchestrator.py`（**新規**: `Session`、`_send_model/_send_bytes/_send_stopped_best_effort`）
- `server/app.py`（`run_server`: load→validate_bind→`create_app(cfg)`→uvicorn）
- `tests/test_ws_tts.py`（**新規**・下記）

## 6. テスト / 検証（codex#3,#13）
- **単体（GPT-SoVITS不要・必須）** `tests/test_ws_tts.py`：
  - `FakeTTSClient`：`async def stream(profile,text)` が `b"\x00\x00"*N` を **2チャンク以上** yield。cancel用に「途中で `await asyncio.sleep()` して待つ」async generator も用意（cancelで途中終了を検証）。
  - `client = TestClient(create_app(cfg, tts_client=fake))`、`with client.websocket_connect("/ws") as ws:` で hello送信→ready受信検証 / speak→audio_begin(text)/binary(bytes)/audio_end / 2連続speakでbusy error(side-band) / cancel→stopped。
  - **注入時 lifespan は client を生成も close もしない**ことを確認（注入clientが使われる）。
  - token: cfg.bind.token 設定時、誤tokenで接続不可（`pytest.raises` で接続失敗 or close を許容）。
- **統合（GPT-SoVITS :9880・任意）**: 小スクリプトで実サーバ接続→first-chunk(TTFA ~150ms)・PCM秒数。
- AST境界・py_compile・既存pytest 全green維持。

## 7. リスク・留意点
- **send一元化**（receiveループと `_stream_tts` の並行send競合）→ `_send_lock`。Lock内で `model_dump_json()` を作らない（Lock外で生成）。
- `cancel`→`active_task.cancel()` のみ。`Stopped` 送信は `_stream_tts` の CancelledError ハンドラに一元化（二重送信防止・codex#11）。
- starlette TestClient の websocket は同期API（`send_json/receive_json/receive_bytes`）。
- `ws.receive()` の戻りは `{"type":"websocket.receive","text":...|"bytes":...}`。text/bytes 分岐。
- profiles_dir 既定 `parents[3]/profiles` を誤らない。active_profile は TOML stem。
- S2向け: `features.auto_tts` は S2で true 化、`stt_*` は S1で `error`。
