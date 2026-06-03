Reading additional input from stdin...
OpenAI Codex v0.135.0
--------
workdir: /mnt/holyland/Project/Text-to-shaberi
model: gpt-5.5
provider: openai
approval: never
sandbox: read-only
reasoning effort: medium
reasoning summaries: none
session id: 019e8b4e-5a06-7d91-9e44-39a1a38dae7a
--------
user
あなたはシニアソフトウェアアーキテクトです。実装計画書 docs/IMPL-S1.md を批判的にレビューしてください。「S1: サーバTTS over WebSocket」の計画で、この後 GLM-5.1 が実装します。

上位設計: docs/ARCH-v0.3-client-server.md(rev2) §4。既存コード: src/text_to_shaberi/server/ws_server.py(現/ws stub), server/config.py, server/tts/client.py(TTSClient.stream/synthesize), server/tts/profile.py, common/protocol.py(pydantic schemas), server/app.py。GPT-SoVITS api_v2 が :9880 稼働。

観点:
1. WS `/ws` ハンドラ設計の正しさ：FastAPI/starlette WebSocket の accept前token検証、receiveループ、send直列化(Lock)。receiveタスクとTTS送信タスクの並行でのrace/順序、binaryフレームのstream帰属
2. ストリーム状態機械：同時1ストリーム強制、speak→audio_begin/binary/audio_end、cancel→stopped、active_task/active_stream_id管理、finallyでの解除タイミング(自分が現行か判定)
3. cancel/CancelledError処理：_stream_tts内でのStopped送信とre-raise、httpx stream(async with)が確実に閉じるか、send Lockとの相互作用(デッドロック懸念)
4. ライフサイクル：lifespanでTTSClient単一生成・aclose、profiles_dir解決(parents[3]/profiles)、available_profiles列挙、app.stateの使い方
5. テスト容易性：starlette TestClientのwebsocketは同期、create_appにtts_client注入口(create_app(config,tts_client))、FakeTTSClientでGPT-SoVITS非依存の単体テストが成立するか。lifespanとtts_client注入の整合(注入時はlifespanで上書きしない)
6. 認証：token query/header、close(1008)のタイミング(accept前にcloseできるか)
7. protocol(parse_message/各モデル)の使い方の正しさ、model_dump_jsonでの送信
8. 受け入れ基準の検証可能性、GLMが迷う曖昧点、抜け

出力はファイル編集せず、Markdownのレビューのみ。各指摘に重要度(高/中/低)と具体的修正提案を付け、最後に総評と「GLM委託前に必ず反映すべきTOP3」を挙げてください。
hook: UserPromptSubmit
hook: UserPromptSubmit
hook: UserPromptSubmit Completed
hook: UserPromptSubmit Completed
codex
まず対象ドキュメントと関連コードを読み、計画が既存の実装境界と噛み合っているか確認します。ファイル編集はせず、レビューだけにします。
exec
/bin/bash -lc "sed -n '1,240p' docs/IMPL-S1.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# S1 実装計画書: サーバ TTS over WebSocket

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2) §4。フロー: 本書 → codexレビュー → GLM委託 → 検証。
> 前提: R0完了（common/server/client分離済み）。GPT-SoVITS api_v2 が :9880 で稼働。**この仕様の通りに実装すること。**

## 1. 目的・スコープ
WSサーバの `/ws` を本実装し、**`speak`(text) → GPT-SoVITS streaming → PCM音声フレーム送信** を実現する。`cancel` 対応とストリーム状態機械（ARCH §4）を作る。
- **スコープ内**: `/ws` ハンドラ、トークン認証、`hello/ready` ハンドシェイク、`speak`→`audio_begin`+binary+`audio_end`、`cancel`→`stopped`、同方向同時1ストリーム強制、エラー処理、TTSClientライフサイクル、サーバ単体検証。
- **スコープ外**: STT/`stt_*`/`transcript`（S2）、Opus（X1, S1はPCM固定）、クライアント実装（C1）。
- **挙動**: クライアント無しで、WSテストクライアント（pytest/スクリプト）から検証できること。

## 2. 既存資産（R0成果・前提）
- `server/ws_server.py`: `create_app()->FastAPI`（`/healthz` ＋ `/ws` stub）。**`/ws` を本実装で置換**。
- `server/config.py`: `ServerConfig`(bind/stt/tts/transport)、`validate_bind`、`load_server_config`。`tts.profiles_dir`・`tts.active_profile`・`bind.token`。
- `server/tts/`: `TTSClient`（`async def stream(profile, text) -> AsyncIterator[bytes]`、`synthesize`）、`load_profile(profiles_dir, name)->TTSProfile`、`TTSProfile.audio`(AudioFormat: encoding/sample_rate/channels)。
- `common/protocol.py`: `Hello/Ready/Speak/Cancel/Stopped/AudioBegin/AudioEnd/ErrorMsg/AudioFormatPayload/ProfileInfo`、`parse_message(dict)->BaseModel`。
- `server/app.py`: `run_server()`（R0 stub）。

## 3. 設計

### 3.1 アプリ構成 / ライフサイクル（`server/ws_server.py` ＋ `server/app.py`）
- `create_app(config: ServerConfig | None = None) -> FastAPI`：config未指定なら `load_server_config(既定パス)`。
- **`profiles_dir` 解決**: `config.tts.profiles_dir` が空なら repo直下 `profiles` を既定にする（`Path(__file__).resolve().parents[3] / "profiles"`）。`app.state` に解決済みパスと config を持たせる。
- **TTSClient はアプリ単一**（GPT-SoVITSへの keep-alive 再利用）: FastAPI `lifespan` で生成し `app.state.tts_client` に格納、shutdownで `await aclose()`。
- `app.state` 例: `config`, `profiles_dir`, `tts_client`, `available_profiles: list[ProfileInfo]`（起動時に `profiles/tts/*.toml` を列挙し meta.name から作成）。
- `server/app.py: run_server()`: `load_server_config` → `validate_bind(config.bind)` → `uvicorn.run(create_app(config), host=config.bind.host, port=config.bind.port)`。CLI `-c server.toml`（既定 `parents[3]/"server.toml"`）。

### 3.2 接続・認証
- `@app.websocket("/ws")` ハンドラ。**accept前にトークン検証**:
  - `config.bind.token` が空 → 認証なしで受理。
  - 非空 → `ws.query_params.get("token")` または `Authorization: Bearer <token>` ヘッダと照合。不一致なら `await ws.close(code=1008)` して終了（acceptしない）。
- 受理後、**接続ごとの `Session`**（§3.3）を生成。

### 3.3 セッション / オーケストレータ（`server/orchestrator.py` 新規）
- `class Session`：1 WS接続の状態を持つ。
  - 属性: `ws`, `app_state`(config/profiles_dir/tts_client/available_profiles), `session_id:str=uuid4().hex`, `active_stream_id: str|None`, `active_task: asyncio.Task|None`。
  - `async def run()`：handshake → メッセージループ → 切断時 cleanup（active_task.cancel()）。
  - **handshake**: 最初に `hello` を受信（型は `parse_message`）。`ready` を返す:
    - `codec="pcm_s16le"`（S1固定）、`tts=AudioFormatPayload(codec="pcm_s16le", sample_rate=active_profile.audio.sample_rate, channels=active_profile.audio.channels)`、`profiles=available_profiles`、`limits={"max_chunk_bytes":65536}`、`features={"auto_tts": false}`（STTはS2）、`server_time=time.time()`。
    - 最初のメッセージが `hello` でなければ `error`（"expected hello"）して継続 or close（S1は close でよい）。
  - **メッセージループ**: `await ws.receive_text()` → `json.loads` → `parse_message`:
    - `Speak`: `await self._handle_speak(msg)`
    - `Cancel`: `await self._handle_cancel(msg)`
    - 不明/未対応(stt_*等): `error`（"unsupported in S1"）。
- `_handle_speak(msg: Speak)`:
  - **同方向同時1ストリーム強制**: `active_task` が未完了なら `error`(id=msg.id, message="busy: stream already active") を返し**新規は受けない**（キューはM-later）。
  - `profile_name = msg.profile or config.tts.active_profile`。`load_profile(profiles_dir, profile_name)`（失敗→`error`）。
  - `active_stream_id = msg.id`、`active_task = asyncio.create_task(self._stream_tts(msg.id, profile, msg.text))`。
- `_stream_tts(stream_id, profile, text)`:
  ```
  fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=profile.audio.sample_rate, channels=profile.audio.channels)
  await ws.send_text(AudioBegin(id=stream_id, format=fmt).model_dump_json())
  try:
      async for chunk in tts_client.stream(profile, text):
          await ws.send_bytes(chunk)
      await ws.send_text(AudioEnd(id=stream_id).model_dump_json())
  except asyncio.CancelledError:
      await ws.send_text(Stopped(id=stream_id).model_dump_json())  # 送れる場合
      raise
  except Exception as exc:
      await ws.send_text(ErrorMsg(id=stream_id, message=repr(exc)).model_dump_json())
  finally:
      # active_* を解除（自分が現行ストリームなら）
  ```
  - **バイナリは現在の `audio_begin`〜`audio_end/error/stopped` の `stream_id` に属する**（ARCH §4）。S1は同時1ストリームなので曖昧さ無し。
- `_handle_cancel(msg: Cancel)`:
  - `active_stream_id == msg.id` かつ `active_task` 生存なら `active_task.cancel()`（`_stream_tts` の CancelledError で `stopped` 送信）。一致しなければ無視 or `error`。
- **送信競合の防止**: 1接続で送信は1コルーチンに直列化（`asyncio.Lock` を持ち、`ws.send_*` を lock 下で行う）。`receive` ループと `_stream_tts` が同時 send しうるため、**send用Lock**を Session に持たせる。

### 3.4 コーデック / フォーマット
- S1は **`pcm_s16le` 固定**。`audio_begin.format` は profile.audio 由来（GPT-SoVITS既定 32000/mono）。
- GPT-SoVITS プロファイルは `media_type="raw", streaming_mode=3`（実測 first-chunk ~150ms）。`TTSClient.stream` が raw PCM チャンクを yield。

## 4. 受け入れ基準
- [ ] `create_app()` を **starlette TestClient の websocket_connect** で叩け、`hello`→`ready`(profiles/codec/tts/features) が返る。
- [ ] `speak` 送信で `audio_begin`→（binary PCMチャンク複数）→`audio_end` が順に届く。受信PCM総バイト/(2*ch*sr) が妥当な秒数。
- [ ] **再生中の2回目 `speak` は `error`(busy)**（同時1ストリーム）。
- [ ] `cancel`(id) で進行中ストリームが止まり `stopped`(id) が届く。GPT-SoVITSへのHTTPもキャンセルされる。
- [ ] token設定時、誤token接続は `close(1008)`、正tokenは疎通。
- [ ] 切断時に active_task がキャンセルされリークしない。
- [ ] py_compile/AST境界テスト（server→PySide6/sounddevice禁止）維持。

## 5. 変更/新規ファイル
- `server/ws_server.py`（`/ws` 本実装、`create_app(config)`、lifespanでTTSClient、profiles列挙）
- `server/orchestrator.py`（**新規**：`Session`）
- `server/app.py`（`run_server` 本実装：config/validate_bind/uvicorn）
- `server/__init__.py`（必要なら export）
- `tests/test_ws_tts.py`（**新規**：handshake/ speak/ busy/ cancel。`TTSClient.stream` を **モック**して GPT-SoVITS非依存の単体テスト＋、実サーバ向けの統合スクリプトは任意）

## 6. テスト / 検証
- **単体（GPT-SoVITS不要・必須）**: `tests/test_ws_tts.py` で `app.state.tts_client` を、固定PCMチャンクを yield する **FakeTTSClient** に差し替え（lifespan前に `app.state` を上書き、または create_app に client注入口を設ける）。starlette TestClient websocket で:
  - hello→ready の各フィールド検証。
  - speak→audio_begin/binary*/audio_end の順序・bytes検証。
  - 2連続speakでbusy error。
  - cancel→stopped。
  - **create_app に `tts_client` 注入引数**（`create_app(config=None, tts_client=None)`）を設け、テスト容易性を確保（未注入時はlifespanで実TTSClient）。
- **統合（GPT-SoVITS :9880 必要・任意）**: `python -m` の小スクリプトで実サーバへ接続し first-chunk(TTFA) 計測（~150ms想定）・PCM秒数確認。
- AST境界テスト・py_compile・既存pytest 全green維持。

## 7. リスク・留意点
- **WS送信の直列化**（receiveループと送信タスクの競合）→ Session の send Lock 必須。
- `cancel` 後の `Stopped` 送信は、ストリーム送信中に別タスクから来るため send Lock で順序確保。CancelledError 中の send は最善努力（切断済みなら無視）。
- TestClient(starlette) の websocket は同期コンテキスト。`receive_json/receive_bytes`/`send_json` を使う。binaryは `receive_bytes`。
- `parse_message` は dict 入力。`ws.receive_text()`→`json.loads`→`parse_message`。
- profiles_dir 既定解決（parents[3]/profiles）を誤らない。
- S2向けメモ: `features.auto_tts` は S1=false、STT実装で true 化。`stt_*` は S1で `error`。

exec
/bin/bash -lc "sed -n '1,260p' docs/ARCH-v0.3-client-server.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# アーキテクチャ改訂 v0.3 — クライアント–サーバ分離（rev2: codexレビュー反映）

> `docs/PLAN.md` v0.2（モノリシック）を改訂。ヒアリング(2026-06-03)で「フロント=Windows(マイク)／処理=ヘッドレスGPUサーバ」が確定。
> **rev2 変更**: codex(gpt-5.5)レビュー(`docs/REVIEW-codex-ARCH-v0.3.md`)を反映＝(1)PCMヘルパー/AudioFormat/protocolを`common`へ・client-only install検証 (2)WSを状態機械化(id/parent_id/stop分割/protocol error) (3)流量制御とWindows音声検証を前倒し (4)Opusは X1 まで保留しPCM固定 (5)VADはwebrtcvad先行 (6)security既定明確化 (7)プロファイルはサーバ取得。
> フロー: 本書 → 各マイルストーン実装計画(`docs/IMPL-*.md`) → GLM委託 → 検証。

## 1. 変更点（モノリシック → 分離）
- 旧: 1プロセスで mic→STT→TTS→再生。
- 新: **Windowsクライアント（GUI/マイク/VAD/再生）** ↔ **WebSocket** ↔ **ヘッドレスGPUサーバ（STT/TTS）**。
- 既存実装(M0/M1/M2/IME)は配置換えして再利用（§8）。

## 2. 全体アーキテクチャ
```
 Windows クライアント                              Linux GPU サーバ (headless)
┌───────────────────────────┐   WebSocket   ┌──────────────────────────────┐
│ PySide6 GUI(+設定:歯車)     │◄─ws(s)://────►│ WS backend (FastAPI/websockets)│
│ マイク取得 + VAD(webrtcvad) │  制御=JSON     │  ├ Orchestrator(セッション/cancel)│
│ テキスト入力 / API入力(LLM) │  音声=binary   │  ├ STT: Qwen3-ASR (GPU常駐)      │
│ StreamingPlayer 再生        │  (X1までPCM)   │  ├ TTS: GPT-SoVITSクライアント   │
│ 出力デバイス選択(VB-CABLE等) │                │  │      (tomlプロファイル)        │
└───────────────────────────┘                │  └ Codec + 認証/TLS             │
                                              └───────────────┬──────────────┘
                                                GPT-SoVITS :9880 (別プロセス・既存)
```

## 3. リポジトリ / 依存の再編
```
src/text_to_shaberi/
├── common/    # protocol.py(pydantic WSスキーマ) / audio_format.py(AudioFormat,CodecFormat) /
│              # pcm.py(_pcm_to_float32,bytes_per_sample,split_full_samples) / config共通
├── server/    # ws_server / orchestrator / stt(Qwen) / tts(GPT-SoVITSクライアント+profile)
├── client/    # ui(PySide6,設定ダイアログ) / mic(sounddevice+webrtcvad) / player(StreamingPlayer) / devices / ws_client
└── (entry)    # text-to-shaberi-server  /  text-to-shaberi-client
```
- **依存グループ**（`[project.optional-dependencies]`）。**トップレベル`dependencies`はcommon最小限(numpy,pydantic)のみ**：
  - `client`: PySide6, qasync, sounddevice, soxr, numpy, websockets, **webrtcvad**（Silero/torchはoptional）, (X1で opus)
  - `server`: torch(cu128), qwen-asr, fastapi, uvicorn, websockets, httpx, numpy, soxr
- **R0受け入れ基準**: `uv sync --extra client` 環境で **torch / qwen_asr を import せず** clientが起動でき、**client配下からserver/STT/TTSをimportしない**（静的チェック）。
- 設定: `server.toml` / `client.toml`（共通ローダ）。TTSプロファイルはサーバ側 `profiles/tts/*.toml`。
- **クライアントUI方針**: メイン画面はシンプル維持（入力・送信・停止・出力先・ログ）。増える設定は**歯車→設定ダイアログ**へ集約。項目: サーバWSアドレス / トークン / 接続(LAN・遠隔) / コーデック(X1〜) / 出力デバイス / **TTSプロファイル選択(サーバから一覧取得)** / 機能トグル(自動TTS,マイクON/OFF) / 将来項目。`client.toml`に`active_profile_id`等を永続化（不在時はserver default）。
- **VAD**: client MVPは **webrtcvad**（torch非依存）。Silero VADはoptional（torch CPU許容時）。§10-4の衝突を解消。

## 4. WebSocket プロトコル v1（状態機械）
制御=テキスト(JSON)、音声=バイナリ。`common/protocol.py`(pydantic)で型共有。
- **ストリーム状態機械**: 各方向(C→S / S→C)で**同時1ストリーム**。stream = `*_begin` → `<binary…>` → `*_end | error | stopped`。**バイナリは現在開いている同方向stream idに属する**。同方向streamを閉じる前の新規`*_begin`は **protocol error**。
- **id**: 発話 `id="u1"`。STT由来の**自動TTSは別id＋`parent_id`**（例 `{"type":"audio_begin","id":"tts-u2","parent_id":"u2"}`）。
- ハンドシェイク:
  - C→S `{"type":"hello","protocol_version":1,"client_name":"...","client_id":"...","caps":{"codecs":["pcm_s16le"]}}`
  - S→C `{"type":"ready","session_id":"...","server_time":...,"codec":"pcm_s16le","tts":{"sample_rate":32000,"channels":1},"profiles":[{"id":"gpt-sovits","name":"..."}],"limits":{"max_chunk_bytes":...,"mic_queue_max_s":...},"features":{"auto_tts":true}}`
- 発話(text→audio): C→S `{"type":"speak","id":"u1","text":"…","profile":"gpt-sovits"}` → S→C `audio_begin`(format) → `<binary>` → `audio_end`。
- STT(mic→text): C→S `{"type":"stt_begin","id":"u2","format":{...},"auto_tts":true}` → `<binary>` → `{"type":"stt_end","id":"u2"}` → S→C `{"type":"transcript","id":"u2","text":"…","final":true}`（+`auto_tts`なら `audio_begin(id="tts-u2",parent_id="u2")…audio_end`）。`stt_begin`中の新規`stt_begin`はサーバが拒否。
- **stop の分割**:
  - `stop_playback`: **クライアントlocal**（再生即abort。サーバへ送らない場合あり）
  - C→S `{"type":"cancel","id":"u1"}`: **サーバstream cancel**（id必須）→ S→C `{"type":"stopped","id":"u1"}`
  - C1の停止ボタン = **local abort 即時 ＋ `cancel` 送信** の両方。
- **エラー2系統**: `connection error`（id無し・致命・切断）と `{"type":"error","id":"u1","message":"…"}`（stream error・継続可）を区別。

## 5. 音声コーデック戦略
- **X1までは `pcm_s16le` 固定**（Windowsのopus依存リスクを実装軸から外す）。Opusは **X1で追加**。
- `common`で **`AudioFormat`(decoded sample_rate/channels/encoding)** と **`CodecFormat`(codec, codec_rate=48k for opus, frame_ms)** を分離。PCMは sample_rate=実音声rate。
- ネゴシエーション(X1〜): サーバ`transport="auto|pcm|opus"`。`auto`は接続元がloopback/プライベートIPならPCM、他はOpus。`hello.caps`と突合し`ready.codec`決定。
- raw/PCMは AudioFormat を `audio_begin` で明示（PLAN §8 原則踏襲）。

## 6. セキュリティ
- **bind既定 `127.0.0.1`**。外部公開は設定 `bind=0.0.0.0` ＋ **token必須**。**token未設定で外部bindなら起動拒否**。
- LAN: token必須（既定で1トークン発行）。遠隔: **wss/TLS ＋ token必須**。
- Origin チェックはブラウザ向け補助。主防御は **token + TLS**。`hello`内token許可時の優先順位を定義。

## 7. レイテンシ予算（ネットワーク＋preroll込み）
TTFAを段階定義: ①server first audio byte ②client first chunk受信 ③playback write開始 ④audible start推定。
- LAN: ②=server first-chunk(148–270ms実測)+net(~5ms)。④≈②+**client preroll(80–150ms, lazy-start修正)**。
  - 148msケース: ~230–300ms → p50≤400ms **余裕**。270msケース: ~350–420ms → **タイト**（preroll下限・低遅延モードで調整）。
- マイク往復 ≈ mic+VAD(端末)+net+STT(0.29s)+TTS first-chunk+net+再生。LANで+10–20ms。
- 遠隔は best-effort（受け入れ基準はLAN主）。

## 8. 既存コードの移行マップ（実在資産 / 新規 / 保留）
**実在資産（配置換え）**
| 既存 | 移行先 |
|---|---|
| `tts/client.py` の `_pcm_to_float32`/`bytes_per_sample`/`split_full_samples`/`AudioBuffer`/`AudioFormat` | **`common/pcm.py`・`common/audio_format.py`**（client/server両用・**最優先**） |
| `tts/client.py` の TTSClient/stream/decode | `server/tts/` |
| `tts/profile.py` | `server/tts/` |
| `audio/player.py`(StreamingPlayer) | `client/audio/`（**lazy-start修正込み**） |
| `audio/devices.py` | `client/audio/` |
| `audio/routing.py`(PipeWire) | `client/audio/`（**Linux/dev用・保留扱い**、Windowsはデバイス選択） |
| `ui/main_window.py` | `client/ui/`（WS送信＋設定ダイアログ結線） |
| `config.py` | `common`＋server/client個別 |
| `app.py`(run_gui/IME) | `client`（run_gui）＋`server`（run_server） |

**新規実装**: `common/protocol.py`(pydantic WSスキーマ) / `server/ws_server`・`server/orchestrator` / `client/ws_client` / `common/codec.py`(X1) / 設定ダイアログ。
**現状stub/未成熟**: `api/server.py`（FastAPI雛形）→ `server/ws_server` の出発点（過大評価しない）。

## 9. 改訂マイルストーン
- **R0 再編**: common(protocol/pcm/audio_format/config) 分離・依存グループ・既存移設・両エントリ空起動。**acceptance: `uv sync --extra client` で torch/qwen 無しに client 起動＋client→server import 無し（静的チェック）**。
- **S1 サーバTTS-WS**: `speak`→GPT-SoVITS streaming→PCMフレーム送信。`cancel`対応。**stream状態機械**実装。
- **C1 クライアント再生**: GUI→WS `speak`→**StreamingPlayer(lazy-start+preroll修正)** 再生。停止=**local abort＋cancel送信**。歯車設定最小版(サーバアドレス/トークン→`client.toml`)。**Windows実機検証(WASAPI再生/VB-CABLE/デバイス切替/stop≤150ms/先頭欠け無し)**。TTFA段階計測。
- **S2 サーバSTT-WS**: 音声→Qwen→`transcript`。**自動TTS(`auto_tts`,`parent_id`)**。`stt_begin`重複拒否。
- **C2 マイク+VAD**: 端末マイク常時録音・**webrtcvad**・発話セグメント→WS→transcript/音声往復。**最低限のバックプレッシャ(mic送信キュー上限秒/超過時は古い音声破棄or録音停止・発話単位ack)**。
- **X1 コーデック/セキュリティ**: **Opus追加**＋token認証＋wss/TLS（遠隔対応）。
- **X2 仕上げ**: WS再接続・設定ダイアログ全項目・ネット込みレイテンシ計測・エラー処理。
> 注: バックプレッシャ/stop/状態機械/Windows検証/lazy-startはX2に溜めず前倒し（codex所見反映）。

## 10. リスク・未決（codexで縮小済）
1. **Windows音声(WASAPI/sounddevice/VB-CABLE)** → **C1で単独実機検証**（後段と原因を混ぜない）。
2. **Opus依存** → X1まで導入しない（PCM先行）。
3. **WSバイナリのid関連付け** → §4で状態機械＋parent_id化により解消。
4. **VAD** → client MVPは webrtcvad（torch非依存）。Silero optional。
5. **再接続/バックプレッシャ** → C2で最低限、X2で本実装。
6. **STT→自動TTS** → **MVPはサーバ内連結**（`auto_tts`フラグ）。クライアント往復TTSは後でoptional。

## 11. 受け入れ基準
**MVP（R0–C2, PCM）**
- [ ] `uv sync --extra client` で torch/qwen 無しに client 起動。
- [ ] サーバ(headless)＋クライアントが ws 接続・`hello/ready`成立。
- [ ] テキスト送信→サーバTTS→クライアント再生（**先頭欠け無し**）。TTFA(LAN)段階計測、audible ≤400ms(p50)。
- [ ] 停止ボタン=local abort即時＋server cancel（音停止＋生成キャンセル ≤150ms）。
- [ ] マイク発話→サーバSTT→transcript→（auto_tts）再生。最低限のバックプレッシャ動作。
- [ ] **設定ダイアログ(歯車)** でサーバアドレス/トークン変更・`client.toml`保存→再接続反映。プロファイル一覧はサーバ取得。
- [ ] **C1 Windows実機**: WASAPI/VB-CABLE再生・切替・stop・先頭欠け無し。
- [ ] 各失敗(接続断/サーバ不達/OOM)でクラッシュせず復帰。

**X1**
- [ ] Opusで疎通（auto切替）。wss/TLS＋token、外部bindでtoken未設定なら起動拒否。

## 12. PLAN.md との関係
- §3 SLO・§7 tomlプロファイル・§5 STT・§8 音声I/O原則は維持（配置がサーバ/クライアントに分かれるだけ）。
- §6 並行設計はクライアント(qasync+mic/再生スレッド)とサーバ(asyncio WS + STTワーカースレッド)に分割。
- 旧 M3–M6 は本書 §9 の R0/S1/C1/S2/C2/X1/X2 に再編。

exec
/bin/bash -lc "sed -n '1,240p' src/text_to_shaberi/server/config.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""サーバ設定 (server.toml) のロード。"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from ..common.config import _only, load_toml


@dataclass(slots=True)
class BindConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""


@dataclass(slots=True)
class QwenLocalConfig:
    model: str = "Qwen/Qwen3-ASR-1.7B"
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    attn_implementation: str = "sdpa"


@dataclass(slots=True)
class STTConfig:
    backend: str = "qwen_local"
    language: str = "ja"
    qwen_local: QwenLocalConfig = field(default_factory=QwenLocalConfig)


@dataclass(slots=True)
class TTSConfig:
    profiles_dir: str = ""
    active_profile: str = "gpt-sovits"


@dataclass(slots=True)
class TransportConfig:
    codec: str = "pcm"


@dataclass(slots=True)
class ServerConfig:
    bind: BindConfig = field(default_factory=BindConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)


def validate_bind(cfg: BindConfig) -> None:
    """host が loopback 以外で token 空なら ValueError。"""
    if cfg.token:
        return
    host = cfg.host
    if host in ("localhost", "::1"):
        return
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            f"bind.host '{host}' は解決不能な hostname です。"
            " 外部公開時は token を設定してください。"
        )
    if addr.is_loopback:
        return
    raise ValueError(
        f"bind.host '{host}' は loopback ではありません。"
        " 外部公開時は bind.token を設定してください。"
    )


def load_server_config(path: str | Any) -> ServerConfig:
    raw = load_toml(path)
    bind_raw = raw.get("bind", {})
    stt_raw = raw.get("stt", {})
    tts_raw = raw.get("tts", {})
    transport_raw = raw.get("transport", {})
    return ServerConfig(
        bind=BindConfig(**_only(bind_raw, BindConfig)),
        stt=STTConfig(
            backend=stt_raw.get("backend", "qwen_local"),
            language=stt_raw.get("language", "ja"),
            qwen_local=QwenLocalConfig(**_only(stt_raw.get("qwen_local", {}), QwenLocalConfig)),
        ),
        tts=TTSConfig(
            profiles_dir=tts_raw.get("profiles_dir", ""),
            active_profile=tts_raw.get("active_profile", "gpt-sovits"),
        ),
        transport=TransportConfig(**_only(transport_raw, TransportConfig)),
    )

exec
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/server/ws_server.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""WS サーバ FastAPI アプリ（R0 stub: /healthz + /ws stub）。

S1 で本実装。create_app() は TestClient で叩ける形に。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """FastAPI アプリを生成する。TestClient で直接叩ける。"""
    app = FastAPI(title="text-to-shaberi-server", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await ws.send_json({"type": "ready", "session_id": "stub"})
            logger.info("[ws] 接続受理（R0 stub・S1で本実装）")
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            logger.info("[ws] クライアント切断")

    return app

codex
計画書自体はかなり具体的ですが、実装者がそのまま書くとぶつかりそうな箇所がいくつかあります。特に lifespan とテスト注入、キャンセル時の送信、プロトコルモデルの実フィールドはコード側をさらに照合します。
exec
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/server/tts/profile.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""TTSプロファイル (profiles/tts/*.toml) のロードとテンプレ展開。

`{{ var }}` を変数で置換する。値全体が単一プレースホルダの場合は変数の型を保持する
（例: streaming_mode のような数値を壊さない）。将来 Jinja2 等へ拡張可能な薄い実装。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from ...common.audio_format import AudioFormat

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


class TTSProfile:
    __slots__ = (
        "name", "driver", "method", "url", "body_type", "timeout_sec",
        "headers", "body", "response_mode", "response_content_type", "audio", "path",
    )

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        method: str,
        url: str,
        body_type: str,
        timeout_sec: float,
        headers: dict[str, str],
        body: dict[str, Any],
        response_mode: str,
        response_content_type: str,
        audio: AudioFormat,
        path: Path | None = None,
    ) -> None:
        self.name = name
        self.driver = driver
        self.method = method
        self.url = url
        self.body_type = body_type
        self.timeout_sec = timeout_sec
        self.headers = headers
        self.body = body
        self.response_mode = response_mode
        self.response_content_type = response_content_type
        self.audio = audio
        self.path = path

    @classmethod
    def load(cls, path: str | Path) -> TTSProfile:
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

exec
/bin/bash -lc "sed -n '1,280p' src/text_to_shaberi/server/tts/client.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""toml駆動の汎用HTTP TTSドライバ（M2: 同期取得＋ストリーミング受信）。

プロファイルの request 仕様で TTS-API を叩き、response.mode と audio フォーマットに
従って音声を numpy float32 [-1,1] へデコードする。M1 はレスポンス全体を受け取ってから
返す。M2 でチャンク逐次受信（stream）を追加し、TTFA を短縮する。
"""
from __future__ import annotations

import base64
import io
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx
import numpy as np

from ...common.audio_format import AudioFormat
from ...common.pcm import AudioBuffer, _pcm_to_float32
from .profile import TTSProfile


def _decode_raw(data: bytes, fmt: AudioFormat) -> AudioBuffer:
    samples = _pcm_to_float32(data, fmt.encoding, fmt.channels)
    return AudioBuffer(samples=samples, sample_rate=fmt.sample_rate)


def _decode_wav(data: bytes) -> AudioBuffer:
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if width == 2:
        arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        arr = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        arr = (np.frombuffer(frames, dtype="u1").astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"未対応のwav sample width: {width}")
    if channels > 1:
        arr = arr.reshape(-1, channels)
    return AudioBuffer(samples=arr, sample_rate=rate)


class TTSClient:
    """1つの httpx.AsyncClient を keep-alive で再利用する。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_request_kwargs(
        self, profile: TTSProfile, body: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        """(method, url, kwargs) を返す。body種別に応じて params/json/data を切替。"""
        kwargs: dict[str, Any] = {"headers": profile.headers, "timeout": profile.timeout_sec}
        if profile.method.upper() == "GET":
            kwargs["params"] = body
        elif profile.body_type == "json":
            kwargs["json"] = body
        elif profile.body_type in ("form", "multipart"):
            kwargs["data"] = body
        else:
            kwargs["json"] = body
        return profile.method, profile.url, kwargs

    def _decode(self, profile: TTSProfile, resp: httpx.Response) -> AudioBuffer:
        data = resp.content
        content_type = resp.headers.get("content-type", "").lower()
        mode = profile.response_mode

        if mode in ("audio_stream", "audio_file"):
            if "wav" in content_type or data[:4] == b"RIFF":
                return _decode_wav(data)
            return _decode_raw(data, profile.audio)
        if mode == "json_base64":
            payload = resp.json()
            field = _extract(payload, profile)
            audio_bytes = base64.b64decode(field)
            if audio_bytes[:4] == b"RIFF":
                return _decode_wav(audio_bytes)
            return _decode_raw(audio_bytes, profile.audio)
        raise ValueError(f"未対応の response.mode: {mode}")

    async def synthesize(self, profile: TTSProfile, text: str, **variables: Any) -> AudioBuffer:
        body = profile.render_body(text, **variables)
        method, url, kwargs = self._build_request_kwargs(profile, body)
        resp = await self._client.request(method, url, **kwargs)
        resp.raise_for_status()
        return self._decode(profile, resp)

    async def stream(
        self, profile: TTSProfile, text: str, **variables: Any
    ) -> AsyncIterator[bytes]:
        """プロファイルに従い HTTP ストリーミング受信 → バイトチャンクを逐次 yield。

        呼び出し側は ``profile.response_mode == "audio_stream"`` を判定してから使うこと。
        Task.cancel() 時は async with が HTTP 接続を確実に閉じる。
        """
        body = profile.render_body(text, **variables)
        method, url, kwargs = self._build_request_kwargs(profile, body)
        async with self._client.stream(method, url, **kwargs) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk


def _extract(payload: dict, profile: TTSProfile) -> str:
    """response.extract.path（ドット区切り）で JSON からフィールドを取り出す（json系用）。"""
    if "audio" in payload:
        return payload["audio"]
    raise ValueError("json_base64 のフィールド抽出に失敗（extract.path 未対応）")

exec
/bin/bash -lc "sed -n '1,220p' src/text_to_shaberi/server/app.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""サーバエントリポイント（R0 stub: 設定ロード + bind検証 + uvicorn起動）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_server_config, validate_bind

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "server.toml"


def run_server(config_path: str | Path) -> None:
    import uvicorn

    cfg = load_server_config(config_path)
    validate_bind(cfg.bind)

    from .ws_server import create_app

    app = create_app()
    uvicorn.run(app, host=cfg.bind.host, port=cfg.bind.port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(prog="text-to-shaberi-server")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="server.toml のパス")
    args = parser.parse_args()
    run_server(args.config)

exec
/bin/bash -lc "sed -n '1,320p' src/text_to_shaberi/common/protocol.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""pydantic v2 WSメッセージスキーマ（型定義のみ）。

状態機械ロジックは持たない（S1/C1）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class AudioFormatPayload(BaseModel):
    codec: str = "pcm_s16le"
    sample_rate: int = 32000
    channels: int = 1


class ProfileInfo(BaseModel):
    id: str
    name: str


class Hello(BaseModel):
    type: Literal["hello"] = "hello"
    protocol_version: int = 1
    client_name: str = ""
    client_id: str = ""
    caps: dict[str, Any] = {}


class Ready(BaseModel):
    type: Literal["ready"] = "ready"
    session_id: str = ""
    server_time: float | None = None
    codec: str = "pcm_s16le"
    tts: AudioFormatPayload = AudioFormatPayload()
    profiles: list[ProfileInfo] = []
    limits: dict[str, Any] = {}
    features: dict[str, Any] = {}


class Speak(BaseModel):
    type: Literal["speak"] = "speak"
    id: str = ""
    text: str = ""
    profile: str | None = None


class SttBegin(BaseModel):
    type: Literal["stt_begin"] = "stt_begin"
    id: str = ""
    format: AudioFormatPayload = AudioFormatPayload()
    auto_tts: bool = True


class SttEnd(BaseModel):
    type: Literal["stt_end"] = "stt_end"
    id: str = ""


class Transcript(BaseModel):
    type: Literal["transcript"] = "transcript"
    id: str = ""
    text: str = ""
    final: bool = True


class AudioBegin(BaseModel):
    type: Literal["audio_begin"] = "audio_begin"
    id: str = ""
    parent_id: str | None = None
    format: AudioFormatPayload = AudioFormatPayload()


class AudioEnd(BaseModel):
    type: Literal["audio_end"] = "audio_end"
    id: str = ""


class Cancel(BaseModel):
    type: Literal["cancel"] = "cancel"
    id: str = ""


class Stopped(BaseModel):
    type: Literal["stopped"] = "stopped"
    id: str = ""


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    id: str | None = None
    message: str = ""


_MESSAGE_TYPES: dict[str, type[BaseModel]] = {
    "hello": Hello,
    "ready": Ready,
    "speak": Speak,
    "stt_begin": SttBegin,
    "stt_end": SttEnd,
    "transcript": Transcript,
    "audio_begin": AudioBegin,
    "audio_end": AudioEnd,
    "cancel": Cancel,
    "stopped": Stopped,
    "error": ErrorMsg,
}


def parse_message(data: dict[str, Any]) -> BaseModel:
    """type フィールドで判別して該当モデルへ変換。"""
    msg_type = data.get("type", "")
    cls = _MESSAGE_TYPES.get(msg_type)
    if cls is None:
        raise ValueError(f"unknown message type: {msg_type}")
    return cls(**{k: v for k, v in data.items() if k != "type" or k in cls.model_fields})

exec
/bin/bash -lc "find profiles -maxdepth 3 -type f -name '*.toml' -print" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
profiles/tts/gpt-sovits.toml

exec
/bin/bash -lc 'rg --files profiles src/text_to_shaberi tests | sort' in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
profiles/tts/gpt-sovits.toml
src/text_to_shaberi/__init__.py
src/text_to_shaberi/__main__.py
src/text_to_shaberi/client/__init__.py
src/text_to_shaberi/client/app.py
src/text_to_shaberi/client/audio/__init__.py
src/text_to_shaberi/client/audio/devices.py
src/text_to_shaberi/client/audio/player.py
src/text_to_shaberi/client/audio/routing.py
src/text_to_shaberi/client/config.py
src/text_to_shaberi/client/ui/__init__.py
src/text_to_shaberi/client/ui/main_window.py
src/text_to_shaberi/client/ws_client.py
src/text_to_shaberi/common/__init__.py
src/text_to_shaberi/common/audio_format.py
src/text_to_shaberi/common/config.py
src/text_to_shaberi/common/pcm.py
src/text_to_shaberi/common/protocol.py
src/text_to_shaberi/server/__init__.py
src/text_to_shaberi/server/app.py
src/text_to_shaberi/server/config.py
src/text_to_shaberi/server/stt/__init__.py
src/text_to_shaberi/server/tts/__init__.py
src/text_to_shaberi/server/tts/client.py
src/text_to_shaberi/server/tts/profile.py
src/text_to_shaberi/server/ws_server.py
tests/test_import_boundaries.py
tests/test_pcm.py

exec
/bin/bash -lc "rg -n \"create_app|TestClient|websocket|protocol|server\" tests src/text_to_shaberi -g '*.py'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
tests/test_import_boundaries.py:3:client/** は server/** / torch / qwen_asr / silero_vad を import 禁止。
tests/test_import_boundaries.py:4:server/** は PySide6 / sounddevice を import 禁止。
tests/test_import_boundaries.py:17:    "text_to_shaberi.server",
tests/test_import_boundaries.py:52:class TestClientImportBoundaries:
tests/test_import_boundaries.py:53:    def test_client_does_not_import_server_or_torch(self) -> None:
tests/test_import_boundaries.py:59:                if forbidden == "text_to_shaberi.server":
tests/test_import_boundaries.py:61:                        assert not m.startswith("text_to_shaberi.server"), (
tests/test_import_boundaries.py:62:                            f"{pyfile.relative_to(_SRC)} imports server: {m}"
tests/test_import_boundaries.py:79:    def test_server_does_not_import_pyside_or_sounddevice(self) -> None:
tests/test_import_boundaries.py:80:        server_dir = _SRC / "server"
tests/test_import_boundaries.py:81:        if not server_dir.exists():
tests/test_import_boundaries.py:82:            pytest.skip("server/ ディレクトリ不在")
tests/test_import_boundaries.py:83:        for pyfile, first_parts, _full_names in _gather_imports(server_dir):
tests/test_import_boundaries.py:92:        from text_to_shaberi.server.config import BindConfig, validate_bind
tests/test_import_boundaries.py:97:        from text_to_shaberi.server.config import BindConfig, validate_bind
tests/test_import_boundaries.py:102:        from text_to_shaberi.server.config import BindConfig, validate_bind
tests/test_import_boundaries.py:108:        from text_to_shaberi.server.config import BindConfig, validate_bind
tests/test_import_boundaries.py:115:        from fastapi.testclient import TestClient
tests/test_import_boundaries.py:117:        from text_to_shaberi.server.ws_server import create_app
tests/test_import_boundaries.py:119:        client = TestClient(create_app())
src/text_to_shaberi/client/app.py:33:    print("  server.address :", cfg.server.address)
src/text_to_shaberi/client/app.py:34:    print("  server.token   :", "(set)" if cfg.server.token else "(empty)")
src/text_to_shaberi/client/app.py:38:    print("  profile.active_id:", cfg.profile.active_id or "(server default)")
src/text_to_shaberi/client/config.py:56:    server: ServerConfig = field(default_factory=ServerConfig)
src/text_to_shaberi/client/config.py:66:        server=ServerConfig(**_only(raw.get("server", {}), ServerConfig)),
src/text_to_shaberi/common/pcm.py:3:旧 tts/client.py から抽出。server TTS と client 再生で共有する。
src/text_to_shaberi/common/protocol.py:25:    protocol_version: int = 1
src/text_to_shaberi/common/protocol.py:34:    server_time: float | None = None
src/text_to_shaberi/server/app.py:8:from .config import load_server_config, validate_bind
src/text_to_shaberi/server/app.py:10:DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "server.toml"
src/text_to_shaberi/server/app.py:13:def run_server(config_path: str | Path) -> None:
src/text_to_shaberi/server/app.py:16:    cfg = load_server_config(config_path)
src/text_to_shaberi/server/app.py:19:    from .ws_server import create_app
src/text_to_shaberi/server/app.py:21:    app = create_app()
src/text_to_shaberi/server/app.py:26:    parser = argparse.ArgumentParser(prog="text-to-shaberi-server")
src/text_to_shaberi/server/app.py:27:    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="server.toml のパス")
src/text_to_shaberi/server/app.py:29:    run_server(args.config)
src/text_to_shaberi/server/config.py:1:"""サーバ設定 (server.toml) のロード。"""
src/text_to_shaberi/server/config.py:74:def load_server_config(path: str | Any) -> ServerConfig:
src/text_to_shaberi/client/ui/main_window.py:4:初期ログは server.address と profile.active_id を表示。
src/text_to_shaberi/client/ui/main_window.py:67:        server_addr = getattr(config, "server", None)
src/text_to_shaberi/client/ui/main_window.py:68:        addr_str = getattr(server_addr, "address", "N/A") if server_addr else "N/A"
src/text_to_shaberi/client/ui/main_window.py:71:        self.log(f"server = {addr_str} / profile = {active_id or '(server default)'}")
src/text_to_shaberi/server/ws_server.py:3:S1 で本実装。create_app() は TestClient で叩ける形に。
src/text_to_shaberi/server/ws_server.py:14:def create_app() -> FastAPI:
src/text_to_shaberi/server/ws_server.py:15:    """FastAPI アプリを生成する。TestClient で直接叩ける。"""
src/text_to_shaberi/server/ws_server.py:16:    app = FastAPI(title="text-to-shaberi-server", version="0.1.0")
src/text_to_shaberi/server/ws_server.py:22:    @app.websocket("/ws")

exec
/bin/bash -lc "sed -n '1,220p' profiles/tts/gpt-sovits.toml" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# GPT-SoVITS (api_v2) 用 TTSプロファイル
# 実測(2026-06-03): streaming_mode=3 で first-chunk 270ms / 出力 pcm_s16le 32000Hz mono
# スキーマ詳細は docs/PLAN.md §7

[meta]
name   = "GPT-SoVITS (ローカル, api_v2)"
driver = "generic_http"

[request]
method      = "POST"
url         = "http://127.0.0.1:9880/tts"
body_type   = "json"               # json | form | multipart | raw_template
timeout_sec = 30
[request.headers]
"Content-Type" = "application/json"

# body はテンプレ。{{ text }} が共通テキストへ置換される（単一プレースホルダは型を保持）
[request.body]
text           = "{{ text }}"
text_lang      = "ja"
ref_audio_path = "/mnt/holyland/Project/Text-to-shaberi/assets/ref.wav"
prompt_text    = "ある条件を満たすと、追ってくるモンスターを逆襲して食べることができる。"
prompt_lang    = "ja"
streaming_mode = 3                 # 0..3 (3=低遅延・実測270ms / 2=中品質439ms)
media_type     = "raw"             # raw=PCMチャンク再生で最小遅延
top_k          = 15
speed_factor   = 1.0

[response]
mode         = "audio_stream"      # audio_stream | audio_file | json_base64 | json_url
content_type = "audio/raw"

[audio]                            # raw/PCM のとき必須（再生フォーマット）
encoding    = "pcm_s16le"
sample_rate = 32000
channels    = 1

exec
/bin/bash -lc "sed -n '1,160p' src/text_to_shaberi/common/config.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""共通設定ローダ helper（_only / tomllib ロード）。"""
from __future__ import annotations

import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any


def _only(d: dict[str, Any], cls: type) -> dict[str, Any]:
    """dataclass のフィールドに存在するキーだけ抽出（未知キーは無視）。"""
    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in valid}


def load_toml(path: str | Path) -> dict[str, Any]:
    """tomllib で TOML ファイルを読み込む。"""
    path = Path(path)
    with path.open("rb") as f:
        return tomllib.load(f)

exec
/bin/bash -lc 'ls -l server.toml 2>/dev/null || true' in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
-rw-rw-r-- 1 misaki_maihara misaki_maihara 748 Jun  3 11:22 server.toml

codex
**レビュー結果**

計画は S1 の主要論点を押さえていますが、GLM に委託する前にいくつか明文化しないと、WebSocket 状態機械とテスト注入まわりで実装が割れる可能性があります。

## 指摘

### 1. 高: `error(busy)` を audio stream 中に挟むかが未定義

計画では再生中の2回目 `speak` に `error(busy)` を返す一方、ARCH §4 は `audio_begin` → `<binary...>` → `audio_end|error|stopped` と読めます。実装が `audio_begin(id=A)` 後に `error(id=B)` を送ると、クライアント実装者が「JSON が来たので A が終端した」と誤解する余地があります。

修正提案:
- S1 仕様に次を明記する。
  - `error(id == active_stream_id)` は active stream の終端。
  - `error(id != active_stream_id)` または `id=None` は side-band control message で、active stream は継続。
  - binary frame は直近の未終端 `audio_begin` の id に属し、side-band JSON では帰属は変わらない。
- または busy error を送る前に active stream を止める方針にする。ただし受け入れ基準上は前者が自然です。

### 2. 高: `close(1008)` を accept 前に期待する仕様が Starlette 挙動とズレる可能性

Starlette/FastAPI では `accept()` 前に `websocket.close()` すると、実運用や TestClient で「WebSocket close code 1008」として観測できず、HTTP denial/403 相当になる場合があります。計画は「accept 前 token 検証」と「close(1008)」を同時に求めていますが、検証方法が曖昧です。

修正提案:
- 認証失敗時の観測仕様を明記する。
  - 実装方針A: accept 前に拒否し、TestClient では接続失敗/denial を受け入れる。close code 1008 の厳密検証はしない。
  - 実装方針B: `accept()` 後に即 `close(1008)` する。ただし accept 前認証ではなくなる。
- セキュリティ観点では A がよいです。受け入れ基準は「誤 token は `/ws` 接続確立不可、または 1008/403 denial として観測」と書くべきです。

### 3. 高: `create_app(config=None, tts_client=None)` と lifespan の整合をもっと厳密に指定すべき

計画後半で注入口に触れていますが、前半の lifespan 設計では常に `app.state.tts_client` を生成するように読めます。これだと TestClient 起動時に FakeTTSClient が上書きされる可能性があります。

修正提案:
- `create_app(config: ServerConfig | None = None, tts_client: TTSClientLike | None = None)` を正式シグネチャにする。
- lifespan では:
  - `tts_client is None` の場合のみ `TTSClient()` を生成し、shutdown で `aclose()`。
  - 注入された client は上書きしない。
  - 注入 client は原則 close しない。close するなら `owns_tts_client` フラグで制御。
- テストは `TestClient(create_app(config, tts_client=fake))` を標準にする。

### 4. 高: `run_server()` が読み込んだ config を `create_app(config)` に渡す点を明文化

既存 `server/app.py` は `cfg = load_server_config(...)` して bind 検証後、`create_app()` を呼んでおり、読み込んだ config がアプリに渡っていません。計画には書かれていますが、GLM が stub を踏襲すると漏れやすい箇所です。

修正提案:
- 変更指示に「`run_server()` は必ず `create_app(cfg)` を呼ぶ。`create_app()` 内で再ロードしない」と明記する。
- `create_app(config=None)` のデフォルトロードは TestClient 便宜用に限定する。

### 5. 中: `available_profiles` の `id` と `name` の定義が不足

計画では `profiles/tts/*.toml` を列挙し `meta.name` から作成とありますが、`Speak.profile` は `load_profile(profiles_dir, name)` に渡され、実際にはファイル stem が profile id です。`meta.name` を id にすると `gpt-sovits.toml` を引けません。

修正提案:
- `ProfileInfo(id=toml_path.stem, name=loaded_profile.name)` と明記。
- `config.tts.active_profile` も `id`、つまり TOML stem として扱う。
- ready の `tts` は active profile をロードして作る。ロード失敗時は起動時エラーか、接続時 `error` 後 close のどちらかを指定。

### 6. 中: `_stream_tts` の `CancelledError` 送信は「最善努力」の具体コードが必要

計画では `Stopped` を送って re-raise とありますが、切断済みや送信失敗時に `WebSocketDisconnect` / `RuntimeError` が出る可能性があります。これを握らないと cancel/切断 cleanup が例外ログまみれになります。

修正提案:
- `send_stopped` は `try/except Exception` で最善努力にする。
- ただし `CancelledError` 自体は re-raise。
- 例:
  - `except asyncio.CancelledError: await self._send_stopped_best_effort(stream_id); raise`
  - `_send_*` は Lock を使い、切断済みなら無視できるようにする。

### 7. 中: `finally` の active 解除タイミングは仕様化されているが、判定条件を明示した方がよい

「自分が現行ストリームなら」とだけあるため、`active_task` と `active_stream_id` のどちらを見るかが曖昧です。

修正提案:
- `task = asyncio.current_task()` を使い、`if self.active_task is task and self.active_stream_id == stream_id:` のときだけ `None` にする、と明記。
- これにより将来キューや再入が入っても古い task が新しい active を消さない。

### 8. 中: `_handle_cancel` の戻り挙動が曖昧

一致しなければ「無視 or error」とありますが、受け入れ基準やクライアント復旧に影響します。

修正提案:
- S1 では決め打ちにする。
  - `id` が active と一致: cancel。
  - active なし / id 不一致: `error(id=msg.id, message="no active stream")` を返す。
- ただし切断 cleanup 由来の cancel では何も送らない。

### 9. 中: `receive_text()` 固定だと binary 誤送信時の protocol error が扱いにくい

S1 では C→S binary は使いませんが、ARCH では将来 STT binary が入ります。`receive_text()` 固定だと binary 受信時に例外扱いになり、きれいな `error` にしにくいです。

修正提案:
- S1 でも `ws.receive()` を使い、`text` なら JSON parse、`bytes` なら `error(message="binary unsupported in S1")` にする。
- 将来 S2 の `stt_begin` binary 受信にも拡張しやすいです。

### 10. 中: parse/validation error の扱いを明記すべき

`json.loads`、`parse_message`、Pydantic validation の失敗時に close するのか `error` を返すのかが未定義です。

修正提案:
- handshake 前:
  - JSON 不正、不明 type、`hello` 以外は `error(id=None, message=...)` 後 close。
- handshake 後:
  - JSON 不正、不明 type は `error(id=None, message=...)` を返して継続、または protocol error として close。S1 では継続の方がテストしやすいです。
- `Speak.id` 空、`Speak.text` 空の扱いも明記する。最低限 `id` 空は `error` 推奨。

### 11. 中: send Lock の運用規約が不足

Lock 必須とありますが、GLM が一部だけ直接 `ws.send_*` すると競合が残ります。

修正提案:
- `Session` に `_send_model(model)`, `_send_bytes(chunk)` のみを用意し、全送信をここ経由にする、と明記。
- Lock 内で長い処理をしない。`model_dump_json()` は Lock 外で作ってから送信する。
- `_handle_cancel` は task cancel だけ行い、`Stopped` 送信は `_stream_tts` に一元化する。

### 12. 低: ready の `features.auto_tts=false` は S1 として妥当だが、ARCH との差分を注記するとよい

ARCH の例では `auto_tts=true` ですが、S1 は STT 未実装なので false は正しいです。

修正提案:
- 「S1 では STT 未実装のため `features.auto_tts=false`。S2 で true 化」と ready 仕様に明記済みなので、受け入れ基準にも false を確認対象として入れる。

### 13. 低: 受け入れ基準の「binary PCMチャンク複数」は Fake 次第で壊れる

FakeTTSClient が1チャンクしか返さないと、基準とテストがズレます。

修正提案:
- 単体テスト用 Fake は `b"\x00\x00" * N` を2チャンク以上 yield する、と明記。
- cancel テスト用 Fake は途中で待機できる async generator にする。

## 総評

S1 の大枠は実装可能です。特に「同時1ストリーム」「TTSClient 単一」「send Lock」「FakeTTSClient による非依存テスト」は正しい方向です。

ただし、現状の計画のままだと GLM は次の3点で迷う可能性が高いです。WebSocket の拒否観測、lifespan 注入、stream 中の side-band error です。ここは実装後に直すより、委託前に仕様を固定した方が安いです。

## GLM委託前に必ず反映すべき TOP3

1. `audio_begin` 中に `error(busy)` を挟めるかを明文化する  
   `error(id != active_stream_id)` は side-band、`error(id == active_stream_id)` は stream 終端、という規則を追加。

2. `create_app(config=None, tts_client=None)` と lifespan の所有権を明文化する  
   注入 client を lifespan で上書きしない。生成した client だけ shutdown で close。

3. token 失敗時の accept 前拒否とテスト期待値を修正する  
   「accept 前に拒否」は維持しつつ、TestClient では 1008 close に限定せず denial/403 相当も許容する基準に変える。
hook: Stop
hook: Stop Completed
tokens used
56,357
**レビュー結果**

計画は S1 の主要論点を押さえていますが、GLM に委託する前にいくつか明文化しないと、WebSocket 状態機械とテスト注入まわりで実装が割れる可能性があります。

## 指摘

### 1. 高: `error(busy)` を audio stream 中に挟むかが未定義

計画では再生中の2回目 `speak` に `error(busy)` を返す一方、ARCH §4 は `audio_begin` → `<binary...>` → `audio_end|error|stopped` と読めます。実装が `audio_begin(id=A)` 後に `error(id=B)` を送ると、クライアント実装者が「JSON が来たので A が終端した」と誤解する余地があります。

修正提案:
- S1 仕様に次を明記する。
  - `error(id == active_stream_id)` は active stream の終端。
  - `error(id != active_stream_id)` または `id=None` は side-band control message で、active stream は継続。
  - binary frame は直近の未終端 `audio_begin` の id に属し、side-band JSON では帰属は変わらない。
- または busy error を送る前に active stream を止める方針にする。ただし受け入れ基準上は前者が自然です。

### 2. 高: `close(1008)` を accept 前に期待する仕様が Starlette 挙動とズレる可能性

Starlette/FastAPI では `accept()` 前に `websocket.close()` すると、実運用や TestClient で「WebSocket close code 1008」として観測できず、HTTP denial/403 相当になる場合があります。計画は「accept 前 token 検証」と「close(1008)」を同時に求めていますが、検証方法が曖昧です。

修正提案:
- 認証失敗時の観測仕様を明記する。
  - 実装方針A: accept 前に拒否し、TestClient では接続失敗/denial を受け入れる。close code 1008 の厳密検証はしない。
  - 実装方針B: `accept()` 後に即 `close(1008)` する。ただし accept 前認証ではなくなる。
- セキュリティ観点では A がよいです。受け入れ基準は「誤 token は `/ws` 接続確立不可、または 1008/403 denial として観測」と書くべきです。

### 3. 高: `create_app(config=None, tts_client=None)` と lifespan の整合をもっと厳密に指定すべき

計画後半で注入口に触れていますが、前半の lifespan 設計では常に `app.state.tts_client` を生成するように読めます。これだと TestClient 起動時に FakeTTSClient が上書きされる可能性があります。

修正提案:
- `create_app(config: ServerConfig | None = None, tts_client: TTSClientLike | None = None)` を正式シグネチャにする。
- lifespan では:
  - `tts_client is None` の場合のみ `TTSClient()` を生成し、shutdown で `aclose()`。
  - 注入された client は上書きしない。
  - 注入 client は原則 close しない。close するなら `owns_tts_client` フラグで制御。
- テストは `TestClient(create_app(config, tts_client=fake))` を標準にする。

### 4. 高: `run_server()` が読み込んだ config を `create_app(config)` に渡す点を明文化

既存 `server/app.py` は `cfg = load_server_config(...)` して bind 検証後、`create_app()` を呼んでおり、読み込んだ config がアプリに渡っていません。計画には書かれていますが、GLM が stub を踏襲すると漏れやすい箇所です。

修正提案:
- 変更指示に「`run_server()` は必ず `create_app(cfg)` を呼ぶ。`create_app()` 内で再ロードしない」と明記する。
- `create_app(config=None)` のデフォルトロードは TestClient 便宜用に限定する。

### 5. 中: `available_profiles` の `id` と `name` の定義が不足

計画では `profiles/tts/*.toml` を列挙し `meta.name` から作成とありますが、`Speak.profile` は `load_profile(profiles_dir, name)` に渡され、実際にはファイル stem が profile id です。`meta.name` を id にすると `gpt-sovits.toml` を引けません。

修正提案:
- `ProfileInfo(id=toml_path.stem, name=loaded_profile.name)` と明記。
- `config.tts.active_profile` も `id`、つまり TOML stem として扱う。
- ready の `tts` は active profile をロードして作る。ロード失敗時は起動時エラーか、接続時 `error` 後 close のどちらかを指定。

### 6. 中: `_stream_tts` の `CancelledError` 送信は「最善努力」の具体コードが必要

計画では `Stopped` を送って re-raise とありますが、切断済みや送信失敗時に `WebSocketDisconnect` / `RuntimeError` が出る可能性があります。これを握らないと cancel/切断 cleanup が例外ログまみれになります。

修正提案:
- `send_stopped` は `try/except Exception` で最善努力にする。
- ただし `CancelledError` 自体は re-raise。
- 例:
  - `except asyncio.CancelledError: await self._send_stopped_best_effort(stream_id); raise`
  - `_send_*` は Lock を使い、切断済みなら無視できるようにする。

### 7. 中: `finally` の active 解除タイミングは仕様化されているが、判定条件を明示した方がよい

「自分が現行ストリームなら」とだけあるため、`active_task` と `active_stream_id` のどちらを見るかが曖昧です。

修正提案:
- `task = asyncio.current_task()` を使い、`if self.active_task is task and self.active_stream_id == stream_id:` のときだけ `None` にする、と明記。
- これにより将来キューや再入が入っても古い task が新しい active を消さない。

### 8. 中: `_handle_cancel` の戻り挙動が曖昧

一致しなければ「無視 or error」とありますが、受け入れ基準やクライアント復旧に影響します。

修正提案:
- S1 では決め打ちにする。
  - `id` が active と一致: cancel。
  - active なし / id 不一致: `error(id=msg.id, message="no active stream")` を返す。
- ただし切断 cleanup 由来の cancel では何も送らない。

### 9. 中: `receive_text()` 固定だと binary 誤送信時の protocol error が扱いにくい

S1 では C→S binary は使いませんが、ARCH では将来 STT binary が入ります。`receive_text()` 固定だと binary 受信時に例外扱いになり、きれいな `error` にしにくいです。

修正提案:
- S1 でも `ws.receive()` を使い、`text` なら JSON parse、`bytes` なら `error(message="binary unsupported in S1")` にする。
- 将来 S2 の `stt_begin` binary 受信にも拡張しやすいです。

### 10. 中: parse/validation error の扱いを明記すべき

`json.loads`、`parse_message`、Pydantic validation の失敗時に close するのか `error` を返すのかが未定義です。

修正提案:
- handshake 前:
  - JSON 不正、不明 type、`hello` 以外は `error(id=None, message=...)` 後 close。
- handshake 後:
  - JSON 不正、不明 type は `error(id=None, message=...)` を返して継続、または protocol error として close。S1 では継続の方がテストしやすいです。
- `Speak.id` 空、`Speak.text` 空の扱いも明記する。最低限 `id` 空は `error` 推奨。

### 11. 中: send Lock の運用規約が不足

Lock 必須とありますが、GLM が一部だけ直接 `ws.send_*` すると競合が残ります。

修正提案:
- `Session` に `_send_model(model)`, `_send_bytes(chunk)` のみを用意し、全送信をここ経由にする、と明記。
- Lock 内で長い処理をしない。`model_dump_json()` は Lock 外で作ってから送信する。
- `_handle_cancel` は task cancel だけ行い、`Stopped` 送信は `_stream_tts` に一元化する。

### 12. 低: ready の `features.auto_tts=false` は S1 として妥当だが、ARCH との差分を注記するとよい

ARCH の例では `auto_tts=true` ですが、S1 は STT 未実装なので false は正しいです。

修正提案:
- 「S1 では STT 未実装のため `features.auto_tts=false`。S2 で true 化」と ready 仕様に明記済みなので、受け入れ基準にも false を確認対象として入れる。

### 13. 低: 受け入れ基準の「binary PCMチャンク複数」は Fake 次第で壊れる

FakeTTSClient が1チャンクしか返さないと、基準とテストがズレます。

修正提案:
- 単体テスト用 Fake は `b"\x00\x00" * N` を2チャンク以上 yield する、と明記。
- cancel テスト用 Fake は途中で待機できる async generator にする。

## 総評

S1 の大枠は実装可能です。特に「同時1ストリーム」「TTSClient 単一」「send Lock」「FakeTTSClient による非依存テスト」は正しい方向です。

ただし、現状の計画のままだと GLM は次の3点で迷う可能性が高いです。WebSocket の拒否観測、lifespan 注入、stream 中の side-band error です。ここは実装後に直すより、委託前に仕様を固定した方が安いです。

## GLM委託前に必ず反映すべき TOP3

1. `audio_begin` 中に `error(busy)` を挟めるかを明文化する  
   `error(id != active_stream_id)` は side-band、`error(id == active_stream_id)` は stream 終端、という規則を追加。

2. `create_app(config=None, tts_client=None)` と lifespan の所有権を明文化する  
   注入 client を lifespan で上書きしない。生成した client だけ shutdown で close。

3. token 失敗時の accept 前拒否とテスト期待値を修正する  
   「accept 前に拒否」は維持しつつ、TestClient では 1008 close に限定せず denial/403 相当も許容する基準に変える。
