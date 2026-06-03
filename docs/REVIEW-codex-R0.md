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
session id: 019e8b39-ea9b-73b0-8b7c-515e00ce73ba
--------
user
あなたはシニアソフトウェアアーキテクトです。実装計画書 docs/IMPL-R0.md を批判的にレビューしてください。これは既存モノリシック実装を common/server/client へ再編する「R0」の計画で、この後 GLM-5.1 が実装します。機械的リファクタですが、取りこぼし・import境界違反・依存グループ設定ミスが起きやすい工程です。

上位設計: docs/ARCH-v0.3-client-server.md (rev2)。既存コード: src/text_to_shaberi/(tts/client.py, tts/profile.py, audio/player.py, audio/devices.py, audio/routing.py, ui/main_window.py, app.py, config.py, api/server.py, __init__.py, __main__.py), pyproject.toml, config.toml, tests/test_pcm.py

観点:
1. 移行マップ(§2,§8)の完全性。既存ファイルの全要素が行き先を持つか。__main__.py や __init__.py の扱い、相対import段数(from ...common.pcm)の正しさ
2. common抽出(§3.1-3.4): pcm/audio_format/protocol/config の責務分割が妥当か。AudioBufferをcommonに置く是非。protocol.py(pydantic)の型定義範囲(R0でどこまで)
3. 依存グループ(§4): uv の optional-dependencies と `uv sync --extra client` の挙動が想定通りか。トップレベルdependenciesを最小化したときのbuild/entry pointへの影響。cu128 index 維持
4. import境界(§6)とその検証テスト(§9 test_import_boundaries)の有効性。sys.modules チェックで torch/qwen 非ロードを確実に検出できるか(遅延importの見逃し)
5. config分割(§3.5,3.7,§5): client.toml/server.toml への割り当て、bind検証、VAD既定をwebrtcに変える妥当性
6. stub範囲(server app/ws_server, client speak/ws_client)がR0として過不足ないか。client speakをstub化してGUIが成立するか
7. 受け入れ基準(§7)の検証可能性。GLMが迷う曖昧点
8. R0で挙動を変えない原則(lazy-startをC1に回す)が守れる計画か

出力はファイル編集せず、Markdownのレビューのみ。各指摘に重要度(高/中/低)と具体的修正提案を付け、最後に総評と「GLM委託前に計画へ必ず反映すべきTOP3」を挙げてください。
hook: UserPromptSubmit
hook: UserPromptSubmit
hook: UserPromptSubmit Completed
hook: UserPromptSubmit Completed
codex
レビュー対象の計画と現行コードの対応関係を確認します。まず設計書、実装計画、既存ファイル、依存設定を読み、移行マップと import 境界を突き合わせます。
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
/bin/bash -lc "sed -n '1,260p' docs/IMPL-R0.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# R0 実装計画書: リポジトリ再編（クライアント–サーバ分離の土台）

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2)。フロー: 本書 → codexレビュー → GLM委託 → 検証。
> 対象: `/mnt/holyland/Project/Text-to-shaberi`（Python3.12 / uv）。**この仕様の通りに実装すること。**

## 1. 目的・スコープ
既存のモノリシック実装(M0/M1/M2/IME)を **common / server / client** に再編し、依存をグループ分離する。
- **スコープ内**: ファイル移設、`common`抽出（PCMヘルパー/AudioFormat/protocolスキーマ/config共通）、依存グループ、2エントリ、import境界、client-only起動検証、テスト更新。
- **スコープ外（後続）**: WS通信の本実装（S1/C1）、STT（S2）、lazy-start修正（C1）。R0では**動作の等価性は問わず、構造の確立と起動可否**を達成する。

## 2. ターゲット構成
```
src/text_to_shaberi/
├── __init__.py                 # 最小化（version等のみ。main再エクスポートは廃止）
├── common/
│   ├── __init__.py
│   ├── pcm.py                  # _pcm_to_float32 / bytes_per_sample / split_full_samples / AudioBuffer
│   ├── audio_format.py         # AudioFormat / CodecFormat
│   ├── protocol.py             # pydantic WSメッセージスキーマ（§3.3）
│   └── config.py               # 共通ローダ helper（_only 等）
├── server/
│   ├── __init__.py
│   ├── config.py               # ServerConfig（server.toml）
│   ├── app.py                  # run_server エントリ（R0はstub: 設定ロード＋/healthzのみ）
│   ├── ws_server.py            # WSサーバ雛形（R0はstub。既存 api/server.py を出発点に）
│   ├── tts/
│   │   ├── __init__.py
│   │   ├── client.py           # TTSClient/stream/synthesize/_decode_*（旧 tts/client.py から移設、PCMはcommon参照）
│   │   └── profile.py          # TTSProfile/load_profile（旧 tts/profile.py 移設）
│   └── stt/
│       └── __init__.py         # S2用プレースホルダ（Qwen設定の置き場だけ用意）
├── client/
│   ├── __init__.py
│   ├── config.py               # ClientConfig（client.toml）
│   ├── app.py                  # run_gui＋IME（旧 app.py から移設）。speakはR0ではstub
│   ├── ws_client.py            # WSクライアント雛形（R0はstub。C1で実装）
│   ├── audio/
│   │   ├── __init__.py
│   │   ├── devices.py          # 旧 audio/devices.py 移設
│   │   ├── player.py           # StreamingPlayer/play（旧 audio/player.py 移設・common.pcm参照）
│   │   └── routing.py          # 旧 audio/routing.py 移設（Linux/dev用・保留扱い）
│   └── ui/
│       ├── __init__.py
│       └── main_window.py      # 旧 ui/main_window.py 移設
└── （旧 tts/ audio/ ui/ api/ config.py app.py は移設に伴い削除）
```

## 3. 詳細

### 3.1 common/pcm.py
旧 `tts/client.py` から **`_pcm_to_float32`・`bytes_per_sample`・`split_full_samples`・`AudioBuffer`** をそのまま移設（ロジック不変）。`server/tts/client.py` と `client/audio/player.py` の双方がここを参照する（**client→server依存を断つ**・codex#1）。

### 3.2 common/audio_format.py
- 既存 `AudioFormat`(encoding, sample_rate, channels) を移設。
- **`CodecFormat`** を新設（X1のOpus用に器だけ用意）: `codec: str="pcm_s16le"`, `codec_rate: int|None=None`(opusは48000), `frame_ms: int|None=None`。R0ではPCM既定のみ使用。

### 3.3 common/protocol.py（pydanticスキーマ・新規）
ARCH §4 のメッセージを pydantic v2 モデルで定義（両側で共有）。`type` 判別の Union。最低限:
- `Hello{type:"hello", protocol_version:int=1, client_name:str, client_id:str, caps:dict}`
- `Ready{type:"ready", session_id:str, codec:str, tts:dict, profiles:list, limits:dict, features:dict}`
- `Speak{type:"speak", id:str, text:str, profile:str|None=None}`
- `SttBegin{type:"stt_begin", id:str, format:dict, auto_tts:bool=True}` / `SttEnd{type:"stt_end", id:str}`
- `Transcript{type:"transcript", id:str, text:str, final:bool=True}`
- `AudioBegin{type:"audio_begin", id:str, parent_id:str|None=None, format:dict}` / `AudioEnd{type:"audio_end", id:str}`
- `Cancel{type:"cancel", id:str}` / `Stopped{type:"stopped", id:str}`
- `ErrorMsg{type:"error", id:str|None=None, message:str}`
- `parse_message(data: dict|str) -> モデル`（type で振り分け）を用意。**ロジックは持たず型定義のみ**（実通信はS1/C1）。

### 3.4 common/config.py
旧 `config.py` の `_only`（dataclassフィールド抽出）等の共通helperを移設。tomllibロードの薄い共通関数。

### 3.5 server/config.py（ServerConfig）
`server.toml` をロード。フィールド:
- `[bind] host="127.0.0.1", port=8765, token=""`
- `[stt] backend="qwen_local", language="ja"`＋`[stt.qwen_local] model/device/dtype/attn_implementation`（旧STTConfig移設）
- `[tts] profiles_dir, active_profile="gpt-sovits"`
- `[transport] codec="pcm"`（R0はpcm固定。auto/opusはX1）
- **bind検証**: `host != "127.0.0.1"` かつ `token==""` なら `ValueError`（起動拒否・codex#11）。R0では検証関数を用意（呼び出しはapp起動時）。

### 3.6 server/app.py・ws_server.py（R0はstub）
- `ws_server.py`: 既存 `api/server.py` の FastAPI 雛形をベースに移設。`/healthz` を残し、WSエンドポイント `/ws` は **stub**（接続受理→`ready`返し→未実装ログでcloseでも可）。本実装はS1。
- `app.py: run_server()`: `ServerConfig`ロード→bind検証→uvicornでws_server起動（既存APIServerパターン）。CLI `-c server.toml`。

### 3.7 client/config.py（ClientConfig）
`client.toml` をロード。フィールド:
- `[server] address="ws://127.0.0.1:8765/ws", token=""`
- `[output] device="default", resample="auto"`
- `[mic] device, sample_rate=16000, channels=1`＋`[mic.preprocess]`＋`[mic.vad] engine="webrtc"`（既定をwebrtcvadに）
- `[ui] input_method="ibus"`
- `[profile] active_id=""`（空=server default）

### 3.8 client/app.py（run_gui＋IME 移設）
- 旧 `app.py` の `run_gui`/`_setup_input_method`/`_load_output_devices`/`run_check`/`main` を移設。import を新パス（`.audio.*`, `.ui.*`, `.config`）へ。
- **speak は R0 では stub**: 送信時に `window.log("[client] WS未接続（C1で実装）")` のみ（`TTSClient`直叩きは廃止。C1で `ws_client` 経由に）。
- `MainWindow` 生成・デバイス列挙・IME・PULSE_PROP はそのまま client 側に残す。
- `client/ws_client.py`: stub（`class WSClient` の空枠だけ。C1で実装）。

### 3.9 server/tts（移設）
- `tts/client.py`→`server/tts/client.py`: `_pcm_to_float32`等を **`from ...common.pcm import`** に変更、`AudioFormat` は `from ...common.audio_format import`。`TTSClient/stream/synthesize/_decode_*` は不変。
- `tts/profile.py`→`server/tts/profile.py`: `AudioFormat` を common参照に。`load_profile` の探索基準（profiles_dir）は ServerConfig 由来に。

### 3.10 client/audio（移設）
- `audio/player.py`→`client/audio/player.py`: PCMヘルパーを **`from ...common.pcm import`**、`AudioFormat`不要なら除去。`StreamingPlayer/play/_device_samplerate` 不変（**lazy-start修正はC1**・R0では現状維持）。
- `audio/devices.py`・`audio/routing.py`→`client/audio/`（不変）。

## 4. pyproject 依存グループ＋エントリ
- **トップレベル `dependencies` を common最小限に**：`numpy`, `pydantic`, （tomllibは標準）。
- `[project.optional-dependencies]`:
  - `client = ["PySide6>=...", "qasync>=...", "sounddevice>=...", "soxr>=...", "numpy>=...", "websockets>=...", "webrtcvad>=..."]`
  - `server = ["torch>=2.11.0", "qwen-asr>=...", "fastapi>=...", "uvicorn>=...", "websockets>=...", "httpx>=...", "numpy>=...", "soxr>=..."]`
  - 旧トップレベルの torch/qwen-asr/silero-vad/pyside6/... は各extraへ移動。`[[tool.uv.index]] cu128` は維持。
- `[project.scripts]`:
  - `text-to-shaberi-client = "text_to_shaberi.client.app:main"`
  - `text-to-shaberi-server = "text_to_shaberi.server.app:main"`
  - `text-to-shaberi = "text_to_shaberi.client.app:main"`（互換エイリアス）
- `[dependency-groups] dev=["pytest>=9.0.3"]` は維持。

## 5. 設定ファイル
- 旧 `config.toml` を **`client.toml` と `server.toml` に分割**（旧 config.toml は削除）。値は §3.5/§3.7 のフィールドに割り当て（既存値を踏襲。VAD engine 既定は `webrtc`）。
- `profiles/tts/gpt-sovits.toml` はそのまま（サーバが参照）。

## 6. import境界（重要・codex#1,#2）
- **`client/**` は `server/**`・`torch`・`qwen_asr` を import してはならない。** common と client 内のみ。
- **`server/**` は `PySide6`・`sounddevice` を import してはならない。**
- `common/**` は torch/PySide6/sounddevice 非依存（numpy/pydantic/標準のみ）。

## 7. 受け入れ基準
- [ ] `py_compile` 全モジュール成功。
- [ ] `tests/` 成功（`test_pcm.py` を `common.pcm` import に更新＋下記境界テスト）。
- [ ] **`uv sync --extra client`**（serverなし）環境で **`uv run text-to-shaberi-client --check` が torch/qwen_asr を import せず**成功（`--check`で設定/プロファイル/デバイス列挙）。
- [ ] `uv run text-to-shaberi-server`（stub）が server.toml をロードし `/healthz` を返す。bind検証（外部bind+token空で起動拒否）。
- [ ] client GUI が起動し、送信で「WS未接続(C1)」ログ（クラッシュなし）。
- [ ] **import境界テスト** green。

## 8. 変更/新規/削除
- 新規: `common/{__init__,pcm,audio_format,protocol,config}.py`, `server/{__init__,config,app,ws_server}.py`, `server/tts/{__init__,client,profile}.py`, `server/stt/__init__.py`, `client/{__init__,config,app,ws_client}.py`, `client/audio/{__init__,devices,player,routing}.py`, `client/ui/{__init__,main_window}.py`, `server.toml`, `client.toml`, `tests/test_import_boundaries.py`
- 移設元削除: `tts/`, `audio/`, `ui/`, `api/`, `config.py`, `app.py`, `__main__.py`(必要なら client へ), `config.toml`
- 変更: `pyproject.toml`, `tests/test_pcm.py`(import更新), `src/text_to_shaberi/__init__.py`(最小化)

## 9. テスト
- `tests/test_pcm.py`: import元を `text_to_shaberi.common.pcm` に更新（テスト内容は不変）。
- `tests/test_import_boundaries.py`（新規）: `import text_to_shaberi.client.app`（および client.audio.*, client.ui.*）後に `"torch" not in sys.modules` と `"qwen_asr" not in sys.modules` を assert。`text_to_shaberi.common.*` import 後に torch/PySide6 非ロードを assert。
- 手動: `uv sync --extra client && uv run text-to-shaberi-client --check`。

## 10. 留意点
- 移設は**ロジック不変**（等価リファクタ）。挙動変更（lazy-start等）はR0で入れない（C1）。
- 相対import段数に注意（`server/tts/client.py` から common は `from ...common.pcm import`）。
- `__init__.py` の再エクスポートで誤って client→server を繋げない。
- `webrtcvad` は R0 では client extra に入れるだけ（使用はC2）。Windowsホイール有無は別途確認。

exec
/bin/bash -lc 'rg --files src/text_to_shaberi pyproject.toml config.toml tests/test_pcm.py' in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
tests/test_pcm.py
config.toml
pyproject.toml
src/text_to_shaberi/app.py
src/text_to_shaberi/__init__.py
src/text_to_shaberi/config.py
src/text_to_shaberi/__main__.py
src/text_to_shaberi/ui/__init__.py
src/text_to_shaberi/ui/main_window.py
src/text_to_shaberi/audio/__init__.py
src/text_to_shaberi/audio/player.py
src/text_to_shaberi/audio/routing.py
src/text_to_shaberi/audio/devices.py
src/text_to_shaberi/tts/__init__.py
src/text_to_shaberi/tts/client.py
src/text_to_shaberi/tts/profile.py
src/text_to_shaberi/api/__init__.py
src/text_to_shaberi/api/server.py

codex
計画書だけだと見えない落とし穴がいくつかありそうなので、次に既存実装のシンボルと import、pyproject の現在値を確認します。特に `app.py` と `config.py` は分割後の実行可能性に直結します。
exec
/bin/bash -lc "sed -n '1,280p' src/text_to_shaberi/tts/client.py" in /mnt/holyland/Project/Text-to-shaberi
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
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from .profile import AudioFormat, TTSProfile


def bytes_per_sample(encoding: str) -> int:
    """PCM encoding 名 → 1サンプルあたりのバイト数。"""
    match encoding:
        case "pcm_s16le":
            return 2
        case "pcm_s32le" | "pcm_f32le":
            return 4
    raise ValueError(f"unsupported encoding: {encoding}")


def split_full_samples(buf: bytes, frame_bytes: int) -> tuple[bytes, bytes]:
    """buf を完全フレーム境界で分割 → (完全フレーム分, 端数carry)。"""
    if frame_bytes <= 0:
        raise ValueError(f"frame_bytes must be positive: {frame_bytes}")
    remainder = len(buf) % frame_bytes
    if remainder == 0:
        return buf, b""
    cut = len(buf) - remainder
    return buf[:cut], buf[cut:]


def _pcm_to_float32(data: bytes, encoding: str, channels: int) -> np.ndarray:
    """生 PCM bytes → float32 ndarray（mono=1D, 多ch=2D）。"""
    match encoding:
        case "pcm_s16le":
            arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        case "pcm_s32le":
            arr = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
        case "pcm_f32le":
            arr = np.frombuffer(data, dtype="<f4").astype(np.float32)
        case _:
            raise ValueError(f"unsupported encoding: {encoding}")
    if channels > 1:
        return arr.reshape(-1, channels)
    return arr


@dataclass(slots=True)
class AudioBuffer:
    samples: np.ndarray  # mono=1D(n,), 多ch=2D(frames,channels), float32 [-1,1]
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate if self.sample_rate else 0.0


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
            # multipartのファイル送信はM2では未対応
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
    # M1ではプロファイルにextract定義が無いので素朴に "audio" を探す。M6で schema 化。
    if "audio" in payload:
        return payload["audio"]
    raise ValueError("json_base64 のフィールド抽出に失敗（extract.path 未対応）")

exec
/bin/bash -lc "sed -n '1,340p' src/text_to_shaberi/app.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""エントリポイント（M2: ストリーミング再生＋音声I/O）。

- `text-to-shaberi`            … GUI 起動
- `text-to-shaberi --check`    … GUIを起動せず設定/プロファイル/デバイスのロードを検査
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .config import AppConfig, load_config
from .tts.profile import load_profile

# リポジトリ直下の config.toml（src/text_to_shaberi/app.py から2つ上）
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.toml"


def _load_output_devices() -> list:
    """sounddevice 列挙。libportaudio2 未導入等で失敗したら空リストで縮退。"""
    try:
        from .audio.devices import list_output_devices

        return list_output_devices()
    except Exception as exc:  # noqa: BLE001 - 起動を止めない
        print(f"[warn] 音声デバイス列挙に失敗: {exc}", file=sys.stderr)
        return []


def run_check(config_path: str | Path) -> None:
    cfg: AppConfig = load_config(config_path)
    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)

    print("== config ==")
    print("  active_tts_profile:", cfg.active_tts_profile)
    print("  stt               :", cfg.stt.backend, "| lang:", cfg.stt.language)
    print("  mic               :", f"{cfg.mic.sample_rate}Hz {cfg.mic.channels}ch vad={cfg.mic.vad.engine}")
    print("  output.device     :", cfg.output.device)
    print("  api               :", f"{cfg.api.host}:{cfg.api.port} enabled={cfg.api.enabled}")
    print("== tts profile ==")
    print("  name  :", profile.name, "| driver:", profile.driver)
    print("  url   :", profile.method, profile.url)
    print("  audio :", profile.audio)
    print("  body  :", profile.render_body("こんにちは、テストです。"))
    print("== output devices ==")
    devices = _load_output_devices()
    for dev in devices:
        print(f"  [{dev.index}] {dev.name} ({int(dev.default_samplerate)}Hz, out={dev.max_output_channels})")
    if not devices:
        print("  (なし / 列挙不可)")


def _setup_input_method(cfg: AppConfig) -> None:
    """QApplication 生成前に日本語入力(IME)を準備する。

    X11+PySide6(Qt6.11)ではシステムの fcitx5 Qt6プラグイン(Qt6.4)が版差で読めないため、
    既定では同梱の ibus プラグイン＋ibus-mozc を使う（アプリだけ ibus、デスクトップは fcitx5 のまま）。
    """
    method = getattr(cfg.ui, "input_method", "ibus")

    if method == "virtualkeyboard":
        os.environ["QT_IM_MODULE"] = "qtvirtualkeyboard"
        print("[ime] Qt仮想キーボードを使用")
        return
    if method != "ibus":
        return  # system: 継承のまま

    import shutil
    import subprocess
    import time

    ibus_daemon = shutil.which("ibus-daemon")
    if ibus_daemon is None:
        print("[ime] ibus-daemon が見つかりません → 入力方式はシステム既定のまま", file=sys.stderr)
        return
    try:
        running = subprocess.run(["pgrep", "-x", "ibus-daemon"], capture_output=True).returncode == 0
        if not running:
            subprocess.Popen([ibus_daemon, "-dr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(12):
                time.sleep(0.15)
                if subprocess.run(["pgrep", "-x", "ibus-daemon"], capture_output=True).returncode == 0:
                    break
            print("[ime] ibus-daemon を起動しました")

        os.environ["QT_IM_MODULE"] = "ibus"
        os.environ["XMODIFIERS"] = "@im=ibus"

        ibus_cli = shutil.which("ibus")
        if ibus_cli:
            for engine in ("mozc-jp", "com.google.IBus.Mozc"):
                if subprocess.run([ibus_cli, "engine", engine], capture_output=True).returncode == 0:
                    print(f"[ime] ibusエンジン: {engine}")
                    break
        print("[ime] 日本語入力=ibus+mozc（トグルキーはmozc/ibus設定に従う）")
    except Exception as exc:  # noqa: BLE001 - IME準備の失敗で起動を止めない
        print(f"[ime] ibusセットアップ失敗（英数のみで継続）: {exc}", file=sys.stderr)


def run_gui(config_path: str | Path) -> None:
    import qasync
    from PySide6.QtWidgets import QApplication

    from .audio.player import StreamingPlayer, play as play_audio
    from .audio.routing import move_app_to_sink
    from .tts.client import TTSClient
    from .ui.main_window import MainWindow

    cfg = load_config(config_path)
    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)

    # PulseAudio クライアント名固定（QApplication 生成前・sounddevice import 前）
    os.environ.setdefault(
        "PULSE_PROP",
        f"application.name=text-to-shaberi media.role=production",
    )

    _setup_input_method(cfg)

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    client = TTSClient()
    window = MainWindow(cfg, output_devices=_load_output_devices())

    current_task: list[asyncio.Task | None] = [None]
    current_player: list[StreamingPlayer | None] = [None]

    async def speak(text: str) -> None:
        device = window.selected_output_device()
        use_virtual = device == "__ttsvirt__"
        play_device = None if use_virtual else device
        window.set_busy(True)
        player: StreamingPlayer | None = None
        try:
            current_task[0] = asyncio.current_task()
            if profile.response_mode == "audio_stream":
                player = await loop.run_in_executor(
                    None,
                    lambda: StreamingPlayer(
                        profile.audio.sample_rate,
                        profile.audio.channels,
                        play_device,
                        profile.audio.encoding,
                    ),
                )
                current_player[0] = player
                if use_virtual:
                    moved = await loop.run_in_executor(
                        None,
                        lambda: move_app_to_sink("ttsvirt", "text-to-shaberi", os.getpid()),
                    )
                    window.log(f"[route] TTS_Virtual move: {moved}")
                t0 = loop.time()
                first_chunk: float | None = None
                async for chunk in client.stream(profile, text):
                    if first_chunk is None:
                        first_chunk = loop.time() - t0
                        window.log(f"[tts] first-chunk {first_chunk * 1000:.0f}ms")
                    await loop.run_in_executor(None, player.write_bytes, chunk)
                await loop.run_in_executor(None, player.close)
                window.log(f"[tts] 再生完了 {loop.time() - t0:.2f}s")
            else:
                t0 = loop.time()
                buf = await client.synthesize(profile, text)
                window.log(f"[tts] {buf.duration:.1f}s 音声を {loop.time() - t0:.2f}s で取得 → 再生")
                await loop.run_in_executor(None, play_audio, buf.samples, buf.sample_rate, play_device)
        except asyncio.CancelledError:
            if player is not None:
                await loop.run_in_executor(None, player.abort)
            window.log("[tts] 停止しました")
        except Exception as exc:
            if player is not None:
                await loop.run_in_executor(None, player.abort)
            window.log(f"[error] {exc!r}")
        finally:
            current_task[0] = None
            current_player[0] = None
            window.set_busy(False)

    def on_speak(text: str) -> None:
        if current_task[0] is not None:
            return
        asyncio.ensure_future(speak(text))

    def on_stop() -> None:
        if current_player[0] is not None:
            loop.run_in_executor(None, current_player[0].abort)
        if current_task[0] is not None:
            current_task[0].cancel()

    window.set_on_speak(on_speak)
    window.set_on_stop(on_stop)

    def on_about_to_quit() -> None:
        if current_task[0] is not None:
            current_task[0].cancel()
        if current_player[0] is not None:
            current_player[0].abort()
        asyncio.ensure_future(client.aclose())

    app.aboutToQuit.connect(on_about_to_quit)
    window.show()

    with loop:
        loop.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(prog="text-to-shaberi")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="config.toml のパス")
    parser.add_argument(
        "--check",
        action="store_true",
        help="GUIを起動せず設定/プロファイル/デバイスのロードを検査して終了",
    )
    args = parser.parse_args()

    if args.check:
        run_check(args.config)
    else:
        run_gui(args.config)

exec
/bin/bash -lc "sed -n '1,260p' pyproject.toml" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
[project]
name = "text-to-shaberi"
version = "0.1.0"
description = "Add your description here"
readme = "README.md"
authors = [
    { name = "May_Kirihara", email = "may.kirihara.the.snow+Cre@gmail.com" }
]
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.136.3",
    "httpx>=0.28.1",
    "numpy>=2.4.4",
    "pyside6>=6.11.1",
    "qasync>=0.28.0",
    "qwen-asr>=0.0.6",
    "silero-vad>=6.2.1",
    "sounddevice>=0.5.5",
    "soxr>=1.1.0",
    "torch>=2.11.0",
    "uvicorn>=0.48.0",
]

[project.scripts]
text-to-shaberi = "text_to_shaberi:main"

[[tool.uv.index]]
url = "https://download.pytorch.org/whl/cu128"

[build-system]
requires = ["uv_build>=0.9.16,<0.10.0"]
build-backend = "uv_build"

[dependency-groups]
dev = [
    "pytest>=9.0.3",
]

exec
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/config.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""アプリ設定 (config.toml) のロード。

未知キーは無視し、欠損キーは既定値で補完する（前方/後方互換のため）。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VADConfig:
    engine: str = "silero"
    min_silence_ms: int = 500
    speech_pad_ms: int = 150


@dataclass(slots=True)
class MicPreprocessConfig:
    gate: bool = True
    denoise: str = "off"


@dataclass(slots=True)
class MicConfig:
    device: str = "default"
    sample_rate: int = 16000
    channels: int = 1
    preprocess: MicPreprocessConfig = field(default_factory=MicPreprocessConfig)
    vad: VADConfig = field(default_factory=VADConfig)


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
class OutputConfig:
    device: str = "default"
    resample: str = "auto"


@dataclass(slots=True)
class APIConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""


@dataclass(slots=True)
class UIConfig:
    # ibus | virtualkeyboard | system
    #  ibus           : アプリだけ ibus+mozc を使い実キーボードで日本語入力（既定）
    #  virtualkeyboard: PySide6同梱の画面キーボード
    #  system         : 環境変数を継承（X11+fcitx5は版差で不可なことに注意）
    input_method: str = "ibus"


@dataclass(slots=True)
class AppConfig:
    active_tts_profile: str = "gpt-sovits"
    mic: MicConfig = field(default_factory=MicConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    api: APIConfig = field(default_factory=APIConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    # 実行時に解決するパス
    config_path: Path | None = None
    profiles_dir: Path | None = None


def _only(d: dict[str, Any], cls: type) -> dict[str, Any]:
    """dataclass のフィールドに存在するキーだけ抽出（未知キーは無視）。"""
    valid = set(cls.__dataclass_fields__)
    return {k: v for k, v in d.items() if k in valid}


def _mic(d: dict[str, Any]) -> MicConfig:
    return MicConfig(
        device=d.get("device", "default"),
        sample_rate=int(d.get("sample_rate", 16000)),
        channels=int(d.get("channels", 1)),
        preprocess=MicPreprocessConfig(**_only(d.get("preprocess", {}), MicPreprocessConfig)),
        vad=VADConfig(**_only(d.get("vad", {}), VADConfig)),
    )


def _stt(d: dict[str, Any]) -> STTConfig:
    return STTConfig(
        backend=d.get("backend", "qwen_local"),
        language=d.get("language", "ja"),
        qwen_local=QwenLocalConfig(**_only(d.get("qwen_local", {}), QwenLocalConfig)),
    )


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)

    app = raw.get("app", {})
    return AppConfig(
        active_tts_profile=app.get("active_tts_profile", "gpt-sovits"),
        mic=_mic(raw.get("mic", {})),
        stt=_stt(raw.get("stt", {})),
        output=OutputConfig(**_only(raw.get("output", {}), OutputConfig)),
        api=APIConfig(**_only(raw.get("api", {}), APIConfig)),
        ui=UIConfig(**_only(raw.get("ui", {}), UIConfig)),
        config_path=path,
        profiles_dir=path.parent / "profiles",
    )

codex
現行 `app.py` は `--check` でも TTS プロファイルを読む作りなので、R0 の「client-only 起動」と衝突しやすいです。残りの UI/audio/API と設定ファイルを見て、stub 化した場合の GUI 成立条件を確認します。
exec
/bin/bash -lc "sed -n '1,280p' src/text_to_shaberi/tts/profile.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""TTSプロファイル (profiles/tts/*.toml) のロードとテンプレ展開。

`{{ var }}` を変数で置換する。値全体が単一プレースホルダの場合は変数の型を保持する
（例: streaming_mode のような数値を壊さない）。将来 Jinja2 等へ拡張可能な薄い実装。
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(slots=True)
class AudioFormat:
    encoding: str = "pcm_s16le"
    sample_rate: int = 32000
    channels: int = 1


@dataclass(slots=True)
class TTSProfile:
    name: str
    driver: str
    method: str
    url: str
    body_type: str
    timeout_sec: float
    headers: dict[str, str]
    body: dict[str, Any]
    response_mode: str
    response_content_type: str
    audio: AudioFormat
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "TTSProfile":
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
/bin/bash -lc "sed -n '1,300p' src/text_to_shaberi/audio/player.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""音声再生（M2: ブロッキング再生＋ストリーミング逐次再生）。

出力デバイスは TTS の出力rate（例 32000Hz）を受け付けないことがある（生のALSA hwデバイス等は
44100/48000固定）。そのため**出力デバイスの既定rateへ soxr で変換**してから再生する。
M2 で OutputStream を使ったチャンク逐次再生（StreamingPlayer）を追加。
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..tts.client import _pcm_to_float32, bytes_per_sample, split_full_samples

logger = logging.getLogger(__name__)


def _device_samplerate(sd, device: int | None) -> int:
    info = sd.query_devices(kind="output") if device is None else sd.query_devices(device)
    return int(info["default_samplerate"])


def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    """samples(float32 [-1,1]) を device で再生し、完了まで待つ（ブロッキング）。

    出力デバイスの既定rateへ必要に応じてリサンプルする。別スレッド(executor)から呼ぶ前提。
    """
    import sounddevice as sd

    if device is None:
        from .devices import preferred_default_output

        device = preferred_default_output()

    target_sr = _device_samplerate(sd, device)
    data = samples
    if target_sr != sample_rate:
        import soxr

        data = soxr.resample(samples, sample_rate, target_sr)
    sd.play(data, samplerate=target_sr, device=device)
    sd.wait()


def stop() -> None:
    import sounddevice as sd

    sd.stop()


class StreamingPlayer:
    """OutputStream + soxr リサンプルでチャンク逐次再生。

    使い方:
        player = StreamingPlayer(src_rate, channels, device, encoding)
        for chunk in http_stream:
            player.write_bytes(chunk)
        player.close()   # 正常終了（末尾まで鳴らす）
        # または player.abort()  # 即停止
    """

    def __init__(
        self,
        src_rate: int,
        channels: int = 1,
        device: int | None = None,
        encoding: str = "pcm_s16le",
        preroll_ms: int = 40,
    ) -> None:
        import sounddevice as sd
        import soxr

        self._encoding = encoding
        self._channels = channels
        self._frame_bytes = bytes_per_sample(encoding) * channels

        if device is None:
            from .devices import preferred_default_output

            device = preferred_default_output()

        self._target_sr = _device_samplerate(sd, device)

        self._resampler: soxr.ResampleStream | None = None
        if self._target_sr != src_rate:
            self._resampler = soxr.ResampleStream(
                src_rate, self._target_sr, channels, dtype="float32", quality="HQ"
            )

        self._stream = sd.OutputStream(
            samplerate=self._target_sr,
            channels=channels,
            dtype="float32",
            device=device,
            latency="low",
        )
        self._stream.start()
        self._closed = False

        self._carry = b""
        self._preroll: list[np.ndarray] = []
        self._preroll_frames = int(self._target_sr * preroll_ms / 1000)
        self._started_audio = False

    def write_bytes(self, data: bytes) -> None:
        """PCM チャンクを受け取り、サンプル境界に整えてから OutputStream へ書き込む。"""
        full, self._carry = split_full_samples(self._carry + data, self._frame_bytes)
        if not full:
            return

        frames = _pcm_to_float32(full, self._encoding, self._channels)

        if self._resampler is not None:
            frames = self._resampler.resample_chunk(frames)
        if len(frames) == 0:
            return

        if not self._started_audio:
            self._preroll.append(frames)
            total = sum(len(f) for f in self._preroll)
            if total >= self._preroll_frames:
                combined = np.concatenate(self._preroll)
                self._preroll.clear()
                try:
                    self._stream.write(combined)
                except Exception:
                    self.abort()
                    raise
                self._started_audio = True
        else:
            try:
                self._stream.write(frames)
            except Exception:
                self.abort()
                raise

    def close(self) -> None:
        """正常終了。resampler 残差を flush し、tail drain 後に stop+close。"""
        if self._closed:
            return
        self._closed = True

        tail: np.ndarray | None = None
        if self._resampler is not None:
            empty = (
                np.empty((0,), dtype=np.float32)
                if self._channels == 1
                else np.empty((0, self._channels), dtype=np.float32)
            )
            tail = self._resampler.resample_chunk(empty, last=True)

        chunks: list[np.ndarray] = []
        if tail is not None and len(tail) > 0:
            chunks.append(tail)
        if self._preroll:
            chunks.extend(self._preroll)
            self._preroll.clear()

        if chunks:
            try:
                self._stream.write(np.concatenate(chunks))
            except Exception:
                pass

        time.sleep(float(self._stream.latency) + 0.05)
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    def abort(self) -> None:
        """即停止（idempotent）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.abort()
        except Exception:
            pass
        try:
            self._stream.close()
        except Exception:
            pass

exec
/bin/bash -lc "sed -n '1,320p' src/text_to_shaberi/ui/main_window.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""メインウィンドウ（M2: テキスト入力・送信・停止・出力デバイス切替・ログ表示）。

M2 で停止ボタン、set_busy（同時発話防止）、TTS_Virtual（配信用）項目を追加。
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class MainWindow(QMainWindow):
    def __init__(self, config, output_devices=None, on_speak: Callable[[str], None] | None = None):
        super().__init__()
        self.config = config
        self._on_speak = on_speak
        self._on_stop: Callable[[], None] | None = None

        self.setWindowTitle("Text-to-shaberi")
        self.resize(680, 460)

        central = QWidget()
        layout = QVBoxLayout(central)

        # 出力デバイス選択（既定 / TTS_Virtual / 各デバイス）
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("出力先:"))
        self.device_combo = QComboBox()
        self.device_combo.addItem("既定デバイス（システム既定sink）", None)
        self.device_combo.addItem("TTS_Virtual（配信用）", "__ttsvirt__")
        for dev in output_devices or []:
            self.device_combo.addItem(f"{dev.name}", dev.index)
        device_row.addWidget(self.device_combo, 1)
        layout.addLayout(device_row)

        # テキスト入力 + 送信 + 停止
        input_row = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("読み上げるテキストを入力…")
        self.send_button = QPushButton("送信")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        input_row.addWidget(self.text_input, 1)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.stop_button)
        layout.addLayout(input_row)

        # ログ
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

        self.setCentralWidget(central)

        self.send_button.clicked.connect(self._handle_send)
        self.text_input.returnPressed.connect(self._handle_send)
        self.stop_button.clicked.connect(self._handle_stop)

        self.log(f"profile = {config.active_tts_profile} / stt = {config.stt.backend}")

    def set_on_speak(self, callback: Callable[[str], None]) -> None:
        self._on_speak = callback

    def set_on_stop(self, callback: Callable[[], None]) -> None:
        self._on_stop = callback

    def selected_output_device(self) -> int | None | str:
        return self.device_combo.currentData()

    def set_busy(self, busy: bool) -> None:
        """送信中UIの有効/無効を切替（同時発話防止）。"""
        self.text_input.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def _handle_send(self) -> None:
        text = self.text_input.text().strip()
        if not text:
            return
        self.text_input.clear()
        self.log(f"[gui] {text}")
        if self._on_speak is not None:
            self._on_speak(text)

    def _handle_stop(self) -> None:
        if self._on_stop is not None:
            self._on_stop()

    def log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

exec
/bin/bash -lc "sed -n '1,220p' src/text_to_shaberi/api/server.py && sed -n '1,120p' src/text_to_shaberi/__init__.py && sed -n '1,120p' src/text_to_shaberi/__main__.py && sed -n '1,220p' config.toml && sed -n '1,220p' tests/test_pcm.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""ローカルAPIサーバ (FastAPI + uvicorn)。

M0: 雛形（/speak, /control, /healthz）と、別スレッドでの起動/graceful shutdown。
WS /stream や割り込みの本実装は M3。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


class SpeakRequest(BaseModel):
    text: str
    profile: str | None = None
    priority: int = 0
    interrupt_policy: str | None = None


class ControlRequest(BaseModel):
    command: str  # stop | cancel | clear


def create_app(
    *,
    on_speak: Callable[[dict], None],
    on_control: Callable[[dict], None],
    auth_token: str = "",
) -> FastAPI:
    """発話/制御コールバックを束ねた FastAPI アプリを生成する。"""
    app = FastAPI(title="text-to-shaberi", version="0.1.0")

    def _authorize(token: str | None) -> None:
        # auth_token が空ならローカル運用としてノーチェック（既定 127.0.0.1）
        if auth_token and token != auth_token:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/speak")
    async def speak(req: SpeakRequest, authorization: str | None = Header(default=None)) -> dict:
        _authorize(authorization)
        on_speak(req.model_dump())
        return {"status": "queued"}

    @app.post("/control")
    async def control(req: ControlRequest, authorization: str | None = Header(default=None)) -> dict:
        _authorize(authorization)
        on_control(req.model_dump())
        return {"status": "ok"}

    return app


class APIServer:
    """uvicorn を別スレッドで起動/停止するラッパ（Qtアプリ内蔵用）。"""

    def __init__(self, app: FastAPI, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.run, name="api-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
"""Text-to-shaberi: マイク/テキスト/API入力 → 任意のTTS-APIサーバ → 音声出力。"""
from .app import main

__all__ = ["main"]
from .app import main

if __name__ == "__main__":
    main()
# Text-to-shaberi アプリ設定
# 詳細は docs/PLAN.md §7.3

[app]
active_tts_profile = "gpt-sovits"   # profiles/tts/<name>.toml

[stt]
backend  = "qwen_local"             # qwen_local | qwen_dashscope
language = "ja"                     # ⇔ Qwen "Japanese"（マッピングは stt 層で固定）
[stt.qwen_local]
model               = "Qwen/Qwen3-ASR-1.7B"
device              = "cuda:0"
dtype               = "bfloat16"
attn_implementation = "sdpa"        # flash-attn導入済みなら "flash_attention_2"

[mic]
device      = "default"
sample_rate = 16000
channels    = 1
[mic.preprocess]
gate    = true                      # 軽量ノイズゲート（MVP既定ON）
denoise = "off"                     # off | rnnoise | noisereduce（任意・遅延計測対象）
[mic.vad]
engine         = "silero"           # silero | webrtc
min_silence_ms = 500                # 発話終端の無音判定
speech_pad_ms  = 150

[output]
device   = "default"                # "default" | デバイス名の一部 | index（仮想オーディオ "TTS_Virtual" 等）
resample = "auto"                   # 出力デバイスrateへ soxr 変換

[api]
enabled    = true
host       = "127.0.0.1"
port       = 8765
auth_token = ""                     # 空=ローカルのみ。外部公開時は必須

[ui]
# 日本語入力の方式（X11+PySide6ではシステムfcitx5プラグインが版差で使えないため）
#   ibus           = アプリだけ ibus+mozc を使い実キーボードで日本語入力（既定）
#   virtualkeyboard = PySide6同梱の画面キーボード
#   system          = 環境変数を継承（通常は英数のみ）
input_method = "ibus"

"""M2: split_full_samples / _pcm_to_float32 ユニットテスト（sounddevice 不要）。"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from text_to_shaberi.tts.client import (
    _pcm_to_float32,
    bytes_per_sample,
    split_full_samples,
)


class TestSplitFullSamples:
    def test_empty(self) -> None:
        full, carry = split_full_samples(b"", 2)
        assert full == b""
        assert carry == b""

    def test_exact_fit(self) -> None:
        full, carry = split_full_samples(b"\x01\x02\x03\x04", 2)
        assert full == b"\x01\x02\x03\x04"
        assert carry == b""

    def test_with_remainder(self) -> None:
        full, carry = split_full_samples(b"\x01\x02\x03", 2)
        assert full == b"\x01\x02"
        assert carry == b"\x03"

    def test_single_byte_remainder(self) -> None:
        full, carry = split_full_samples(b"\xAA", 2)
        assert full == b""
        assert carry == b"\xAA"

    def test_frame_bytes_4(self) -> None:
        data = b"\x01\x02\x03\x04\x05\x06\x07"
        full, carry = split_full_samples(data, 4)
        assert full == b"\x01\x02\x03\x04"
        assert carry == b"\x05\x06\x07"

    def test_invalid_frame_bytes(self) -> None:
        with pytest.raises(ValueError):
            split_full_samples(b"\x01", 0)


class TestBytesPerSample:
    def test_s16le(self) -> None:
        assert bytes_per_sample("pcm_s16le") == 2

    def test_s32le(self) -> None:
        assert bytes_per_sample("pcm_s32le") == 4

    def test_f32le(self) -> None:
        assert bytes_per_sample("pcm_f32le") == 4

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported encoding"):
            bytes_per_sample("pcm_s24le")


class TestPcmToFloat32:
    def test_s16le_mono_1d(self) -> None:
        raw = struct.pack("<hh", 0, 32767)
        out = _pcm_to_float32(raw, "pcm_s16le", 1)
        assert out.ndim == 1
        np.testing.assert_allclose(out, [0.0, 32767 / 32768], atol=1e-7)

    def test_s16le_2ch_2d(self) -> None:
        raw = struct.pack("<hhhh", 1000, -1000, 0, 32767)
        out = _pcm_to_float32(raw, "pcm_s16le", 2)
        assert out.shape == (2, 2)
        np.testing.assert_allclose(out[0], [1000 / 32768, -1000 / 32768], atol=1e-7)

    def test_s32le_mono(self) -> None:
        raw = struct.pack("<i", 2147483647)
        out = _pcm_to_float32(raw, "pcm_s32le", 1)
        np.testing.assert_allclose(out, [2147483647 / 2147483648], atol=1e-7)

    def test_f32le_mono_passthrough(self) -> None:
        raw = struct.pack("<ff", 0.5, -0.25)
        out = _pcm_to_float32(raw, "pcm_f32le", 1)
        assert out.ndim == 1
        np.testing.assert_allclose(out, [0.5, -0.25], atol=1e-7)

    def test_unknown_encoding_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported encoding"):
            _pcm_to_float32(b"\x00\x00", "pcm_s24le", 1)

codex
ここまでで高リスク箇所は、`client.app --check` がサーバ側プロファイルを触る点、`MainWindow` が旧 `AppConfig` 属性に依存している点、依存 extras のテスト方法が少し弱い点です。最後に import の静的検証案と config 分割の現実性を詰めます。
exec
/bin/bash -lc "sed -n '1,220p' src/text_to_shaberi/audio/routing.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
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

exec
/bin/bash -lc "sed -n '1,220p' src/text_to_shaberi/audio/devices.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""音声デバイス列挙 (sounddevice/PortAudio)。

`sounddevice` は libportaudio2 が無いと import 時点で失敗するため遅延importする。
列挙に失敗した場合は呼び出し側で握りつぶせるよう、例外をそのまま送出する。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float


def _sd():
    import sounddevice as sd  # 遅延import（libportaudio2 が必要）

    return sd


def list_devices(kind: str | None = None) -> list[AudioDevice]:
    """kind: None=全部 / "output" / "input"。"""
    sd = _sd()
    result: list[AudioDevice] = []
    for index, d in enumerate(sd.query_devices()):
        dev = AudioDevice(
            index=index,
            name=d["name"],
            max_input_channels=int(d["max_input_channels"]),
            max_output_channels=int(d["max_output_channels"]),
            default_samplerate=float(d["default_samplerate"]),
        )
        if kind == "output" and dev.max_output_channels <= 0:
            continue
        if kind == "input" and dev.max_input_channels <= 0:
            continue
        result.append(dev)
    return result


def list_output_devices() -> list[AudioDevice]:
    return list_devices("output")


def list_input_devices() -> list[AudioDevice]:
    return list_devices("input")


# 「既定」選択時に優先する出力デバイス名（PipeWire/pulse はシステム既定sinkへ流れる）。
# PortAudio の device=None は生hw（未接続アナログ等）を指すことがあり無音になりやすい。
_PREFERRED_DEFAULT_NAMES = ("pipewire", "pulse", "default", "sysdefault")


def preferred_default_output() -> int | None:
    """システム既定sinkへ確実に届く出力デバイスindexを推定。

    PipeWire/pulse 系を優先する。見つからなければ None（PortAudio既定にフォールバック）。
    """
    try:
        devices = list_output_devices()
    except Exception:  # noqa: BLE001 - 列挙不可ならフォールバック
        return None
    pairs = [(d.index, d.name.lower()) for d in devices]
    for target in _PREFERRED_DEFAULT_NAMES:
        for index, name in pairs:
            if name == target:
                return index
    for index, name in pairs:
        if "pipewire" in name or "pulse" in name:
            return index
    return None


def resolve_device(name_or_default: str | int | None, kind: str = "output") -> int | None:
    """設定値（'default' / index / 名前の一部）を PortAudio デバイスindexへ解決。

    'default'/None/'' は None（=PortAudio既定）を返す。
    """
    if name_or_default in (None, "", "default"):
        return None
    try:
        return int(name_or_default)  # index 指定
    except (TypeError, ValueError):
        pass
    needle = str(name_or_default).lower()
    for dev in list_devices(kind):
        if needle in dev.name.lower():
            return dev.index
    raise ValueError(f"音声デバイスが見つかりません: {name_or_default!r}")

exec
/bin/bash -lc "rg -n \"from text_to_shaberi|from \\.|import text_to_shaberi|import torch|qwen|silero|sounddevice|PySide6|httpx|fastapi|uvicorn|active_tts_profile|cfg\\.stt|cfg\\.api\" src/text_to_shaberi tests pyproject.toml config.toml" in /mnt/holyland/Project/Text-to-shaberi
exec
/bin/bash -lc "find . -maxdepth 4 -type f | sort | rg 'profiles|toml|lock'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
config.toml:5:active_tts_profile = "gpt-sovits"   # profiles/tts/<name>.toml
config.toml:8:backend  = "qwen_local"             # qwen_local | qwen_dashscope
config.toml:10:[stt.qwen_local]
config.toml:24:engine         = "silero"           # silero | webrtc
config.toml:39:# 日本語入力の方式（X11+PySide6ではシステムfcitx5プラグインが版差で使えないため）
config.toml:41:#   virtualkeyboard = PySide6同梱の画面キーボード
pyproject.toml:11:    "fastapi>=0.136.3",
pyproject.toml:12:    "httpx>=0.28.1",
pyproject.toml:16:    "qwen-asr>=0.0.6",
pyproject.toml:17:    "silero-vad>=6.2.1",
pyproject.toml:18:    "sounddevice>=0.5.5",
pyproject.toml:21:    "uvicorn>=0.48.0",
tests/test_pcm.py:1:"""M2: split_full_samples / _pcm_to_float32 ユニットテスト（sounddevice 不要）。"""
tests/test_pcm.py:9:from text_to_shaberi.tts.client import (
src/text_to_shaberi/app.py:14:from .config import AppConfig, load_config
src/text_to_shaberi/app.py:15:from .tts.profile import load_profile
src/text_to_shaberi/app.py:22:    """sounddevice 列挙。libportaudio2 未導入等で失敗したら空リストで縮退。"""
src/text_to_shaberi/app.py:24:        from .audio.devices import list_output_devices
src/text_to_shaberi/app.py:34:    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)
src/text_to_shaberi/app.py:37:    print("  active_tts_profile:", cfg.active_tts_profile)
src/text_to_shaberi/app.py:38:    print("  stt               :", cfg.stt.backend, "| lang:", cfg.stt.language)
src/text_to_shaberi/app.py:41:    print("  api               :", f"{cfg.api.host}:{cfg.api.port} enabled={cfg.api.enabled}")
src/text_to_shaberi/app.py:58:    X11+PySide6(Qt6.11)ではシステムの fcitx5 Qt6プラグイン(Qt6.4)が版差で読めないため、
src/text_to_shaberi/app.py:104:    from PySide6.QtWidgets import QApplication
src/text_to_shaberi/app.py:106:    from .audio.player import StreamingPlayer, play as play_audio
src/text_to_shaberi/app.py:107:    from .audio.routing import move_app_to_sink
src/text_to_shaberi/app.py:108:    from .tts.client import TTSClient
src/text_to_shaberi/app.py:109:    from .ui.main_window import MainWindow
src/text_to_shaberi/app.py:112:    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)
src/text_to_shaberi/app.py:114:    # PulseAudio クライアント名固定（QApplication 生成前・sounddevice import 前）
src/text_to_shaberi/__init__.py:2:from .app import main
src/text_to_shaberi/config.py:15:    engine: str = "silero"
src/text_to_shaberi/config.py:45:    backend: str = "qwen_local"
src/text_to_shaberi/config.py:47:    qwen_local: QwenLocalConfig = field(default_factory=QwenLocalConfig)
src/text_to_shaberi/config.py:68:    #  virtualkeyboard: PySide6同梱の画面キーボード
src/text_to_shaberi/config.py:75:    active_tts_profile: str = "gpt-sovits"
src/text_to_shaberi/config.py:104:        backend=d.get("backend", "qwen_local"),
src/text_to_shaberi/config.py:106:        qwen_local=QwenLocalConfig(**_only(d.get("qwen_local", {}), QwenLocalConfig)),
src/text_to_shaberi/config.py:117:        active_tts_profile=app.get("active_tts_profile", "gpt-sovits"),
src/text_to_shaberi/__main__.py:1:from .app import main
src/text_to_shaberi/api/__init__.py:2:from .server import APIServer, ControlRequest, SpeakRequest, create_app
src/text_to_shaberi/tts/__init__.py:2:from .profile import AudioFormat, TTSProfile, load_profile, render_value
src/text_to_shaberi/api/server.py:1:"""ローカルAPIサーバ (FastAPI + uvicorn)。
src/text_to_shaberi/api/server.py:11:import uvicorn
src/text_to_shaberi/api/server.py:12:from fastapi import FastAPI, Header, HTTPException
src/text_to_shaberi/api/server.py:61:    """uvicorn を別スレッドで起動/停止するラッパ（Qtアプリ内蔵用）。"""
src/text_to_shaberi/api/server.py:64:        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
src/text_to_shaberi/api/server.py:65:        self._server = uvicorn.Server(self._config)
src/text_to_shaberi/tts/client.py:16:import httpx
src/text_to_shaberi/tts/client.py:19:from .profile import AudioFormat, TTSProfile
src/text_to_shaberi/tts/client.py:94:    """1つの httpx.AsyncClient を keep-alive で再利用する。"""
src/text_to_shaberi/tts/client.py:97:        self._client = httpx.AsyncClient()
src/text_to_shaberi/tts/client.py:118:    def _decode(self, profile: TTSProfile, resp: httpx.Response) -> AudioBuffer:
src/text_to_shaberi/ui/__init__.py:1:"""GUI 層 (PySide6)。"""
src/text_to_shaberi/ui/__init__.py:2:from .main_window import MainWindow
src/text_to_shaberi/audio/__init__.py:2:from .devices import (
src/text_to_shaberi/audio/__init__.py:9:from .player import StreamingPlayer, play, stop
src/text_to_shaberi/ui/main_window.py:9:from PySide6.QtWidgets import (
src/text_to_shaberi/ui/main_window.py:69:        self.log(f"profile = {config.active_tts_profile} / stt = {config.stt.backend}")
src/text_to_shaberi/audio/devices.py:1:"""音声デバイス列挙 (sounddevice/PortAudio)。
src/text_to_shaberi/audio/devices.py:3:`sounddevice` は libportaudio2 が無いと import 時点で失敗するため遅延importする。
src/text_to_shaberi/audio/devices.py:21:    import sounddevice as sd  # 遅延import（libportaudio2 が必要）
src/text_to_shaberi/audio/player.py:14:from ..tts.client import _pcm_to_float32, bytes_per_sample, split_full_samples
src/text_to_shaberi/audio/player.py:29:    import sounddevice as sd
src/text_to_shaberi/audio/player.py:32:        from .devices import preferred_default_output
src/text_to_shaberi/audio/player.py:47:    import sounddevice as sd
src/text_to_shaberi/audio/player.py:71:        import sounddevice as sd
src/text_to_shaberi/audio/player.py:79:            from .devices import preferred_default_output

 succeeded in 0ms:
./.venv/.lock
./config.toml
./profiles/tts/gpt-sovits.toml
./pyproject.toml
./uv.lock

codex
移行マップ上は「ディレクトリごと移設」で済むように見えますが、`__init__.py` の再エクスポートが境界違反を作る可能性があります。ここを最終確認して、レビュー指摘としてまとめます。
exec
/bin/bash -lc "sed -n '1,120p' src/text_to_shaberi/audio/__init__.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""音声I/O 層：デバイス列挙、再生、ルーティング。"""
from .devices import (
    AudioDevice,
    list_input_devices,
    list_output_devices,
    preferred_default_output,
    resolve_device,
)
from .player import StreamingPlayer, play, stop

__all__ = [
    "AudioDevice",
    "list_input_devices",
    "list_output_devices",
    "preferred_default_output",
    "resolve_device",
    "play",
    "stop",
    "StreamingPlayer",
]

exec
/bin/bash -lc "sed -n '1,120p' src/text_to_shaberi/tts/__init__.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""TTS 層：toml駆動の汎用HTTPドライバとプロファイル。"""
from .profile import AudioFormat, TTSProfile, load_profile, render_value

__all__ = ["AudioFormat", "TTSProfile", "load_profile", "render_value"]

exec
/bin/bash -lc "sed -n '1,120p' src/text_to_shaberi/api/__init__.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""API 入力層：LLM等からの発話投入を受けるローカルHTTPサーバ。"""
from .server import APIServer, ControlRequest, SpeakRequest, create_app

__all__ = ["APIServer", "ControlRequest", "SpeakRequest", "create_app"]

exec
/bin/bash -lc "nl -ba docs/IMPL-R0.md | sed -n '1,240p'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
     1	# R0 実装計画書: リポジトリ再編（クライアント–サーバ分離の土台）
     2	
     3	> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2)。フロー: 本書 → codexレビュー → GLM委託 → 検証。
     4	> 対象: `/mnt/holyland/Project/Text-to-shaberi`（Python3.12 / uv）。**この仕様の通りに実装すること。**
     5	
     6	## 1. 目的・スコープ
     7	既存のモノリシック実装(M0/M1/M2/IME)を **common / server / client** に再編し、依存をグループ分離する。
     8	- **スコープ内**: ファイル移設、`common`抽出（PCMヘルパー/AudioFormat/protocolスキーマ/config共通）、依存グループ、2エントリ、import境界、client-only起動検証、テスト更新。
     9	- **スコープ外（後続）**: WS通信の本実装（S1/C1）、STT（S2）、lazy-start修正（C1）。R0では**動作の等価性は問わず、構造の確立と起動可否**を達成する。
    10	
    11	## 2. ターゲット構成
    12	```
    13	src/text_to_shaberi/
    14	├── __init__.py                 # 最小化（version等のみ。main再エクスポートは廃止）
    15	├── common/
    16	│   ├── __init__.py
    17	│   ├── pcm.py                  # _pcm_to_float32 / bytes_per_sample / split_full_samples / AudioBuffer
    18	│   ├── audio_format.py         # AudioFormat / CodecFormat
    19	│   ├── protocol.py             # pydantic WSメッセージスキーマ（§3.3）
    20	│   └── config.py               # 共通ローダ helper（_only 等）
    21	├── server/
    22	│   ├── __init__.py
    23	│   ├── config.py               # ServerConfig（server.toml）
    24	│   ├── app.py                  # run_server エントリ（R0はstub: 設定ロード＋/healthzのみ）
    25	│   ├── ws_server.py            # WSサーバ雛形（R0はstub。既存 api/server.py を出発点に）
    26	│   ├── tts/
    27	│   │   ├── __init__.py
    28	│   │   ├── client.py           # TTSClient/stream/synthesize/_decode_*（旧 tts/client.py から移設、PCMはcommon参照）
    29	│   │   └── profile.py          # TTSProfile/load_profile（旧 tts/profile.py 移設）
    30	│   └── stt/
    31	│       └── __init__.py         # S2用プレースホルダ（Qwen設定の置き場だけ用意）
    32	├── client/
    33	│   ├── __init__.py
    34	│   ├── config.py               # ClientConfig（client.toml）
    35	│   ├── app.py                  # run_gui＋IME（旧 app.py から移設）。speakはR0ではstub
    36	│   ├── ws_client.py            # WSクライアント雛形（R0はstub。C1で実装）
    37	│   ├── audio/
    38	│   │   ├── __init__.py
    39	│   │   ├── devices.py          # 旧 audio/devices.py 移設
    40	│   │   ├── player.py           # StreamingPlayer/play（旧 audio/player.py 移設・common.pcm参照）
    41	│   │   └── routing.py          # 旧 audio/routing.py 移設（Linux/dev用・保留扱い）
    42	│   └── ui/
    43	│       ├── __init__.py
    44	│       └── main_window.py      # 旧 ui/main_window.py 移設
    45	└── （旧 tts/ audio/ ui/ api/ config.py app.py は移設に伴い削除）
    46	```
    47	
    48	## 3. 詳細
    49	
    50	### 3.1 common/pcm.py
    51	旧 `tts/client.py` から **`_pcm_to_float32`・`bytes_per_sample`・`split_full_samples`・`AudioBuffer`** をそのまま移設（ロジック不変）。`server/tts/client.py` と `client/audio/player.py` の双方がここを参照する（**client→server依存を断つ**・codex#1）。
    52	
    53	### 3.2 common/audio_format.py
    54	- 既存 `AudioFormat`(encoding, sample_rate, channels) を移設。
    55	- **`CodecFormat`** を新設（X1のOpus用に器だけ用意）: `codec: str="pcm_s16le"`, `codec_rate: int|None=None`(opusは48000), `frame_ms: int|None=None`。R0ではPCM既定のみ使用。
    56	
    57	### 3.3 common/protocol.py（pydanticスキーマ・新規）
    58	ARCH §4 のメッセージを pydantic v2 モデルで定義（両側で共有）。`type` 判別の Union。最低限:
    59	- `Hello{type:"hello", protocol_version:int=1, client_name:str, client_id:str, caps:dict}`
    60	- `Ready{type:"ready", session_id:str, codec:str, tts:dict, profiles:list, limits:dict, features:dict}`
    61	- `Speak{type:"speak", id:str, text:str, profile:str|None=None}`
    62	- `SttBegin{type:"stt_begin", id:str, format:dict, auto_tts:bool=True}` / `SttEnd{type:"stt_end", id:str}`
    63	- `Transcript{type:"transcript", id:str, text:str, final:bool=True}`
    64	- `AudioBegin{type:"audio_begin", id:str, parent_id:str|None=None, format:dict}` / `AudioEnd{type:"audio_end", id:str}`
    65	- `Cancel{type:"cancel", id:str}` / `Stopped{type:"stopped", id:str}`
    66	- `ErrorMsg{type:"error", id:str|None=None, message:str}`
    67	- `parse_message(data: dict|str) -> モデル`（type で振り分け）を用意。**ロジックは持たず型定義のみ**（実通信はS1/C1）。
    68	
    69	### 3.4 common/config.py
    70	旧 `config.py` の `_only`（dataclassフィールド抽出）等の共通helperを移設。tomllibロードの薄い共通関数。
    71	
    72	### 3.5 server/config.py（ServerConfig）
    73	`server.toml` をロード。フィールド:
    74	- `[bind] host="127.0.0.1", port=8765, token=""`
    75	- `[stt] backend="qwen_local", language="ja"`＋`[stt.qwen_local] model/device/dtype/attn_implementation`（旧STTConfig移設）
    76	- `[tts] profiles_dir, active_profile="gpt-sovits"`
    77	- `[transport] codec="pcm"`（R0はpcm固定。auto/opusはX1）
    78	- **bind検証**: `host != "127.0.0.1"` かつ `token==""` なら `ValueError`（起動拒否・codex#11）。R0では検証関数を用意（呼び出しはapp起動時）。
    79	
    80	### 3.6 server/app.py・ws_server.py（R0はstub）
    81	- `ws_server.py`: 既存 `api/server.py` の FastAPI 雛形をベースに移設。`/healthz` を残し、WSエンドポイント `/ws` は **stub**（接続受理→`ready`返し→未実装ログでcloseでも可）。本実装はS1。
    82	- `app.py: run_server()`: `ServerConfig`ロード→bind検証→uvicornでws_server起動（既存APIServerパターン）。CLI `-c server.toml`。
    83	
    84	### 3.7 client/config.py（ClientConfig）
    85	`client.toml` をロード。フィールド:
    86	- `[server] address="ws://127.0.0.1:8765/ws", token=""`
    87	- `[output] device="default", resample="auto"`
    88	- `[mic] device, sample_rate=16000, channels=1`＋`[mic.preprocess]`＋`[mic.vad] engine="webrtc"`（既定をwebrtcvadに）
    89	- `[ui] input_method="ibus"`
    90	- `[profile] active_id=""`（空=server default）
    91	
    92	### 3.8 client/app.py（run_gui＋IME 移設）
    93	- 旧 `app.py` の `run_gui`/`_setup_input_method`/`_load_output_devices`/`run_check`/`main` を移設。import を新パス（`.audio.*`, `.ui.*`, `.config`）へ。
    94	- **speak は R0 では stub**: 送信時に `window.log("[client] WS未接続（C1で実装）")` のみ（`TTSClient`直叩きは廃止。C1で `ws_client` 経由に）。
    95	- `MainWindow` 生成・デバイス列挙・IME・PULSE_PROP はそのまま client 側に残す。
    96	- `client/ws_client.py`: stub（`class WSClient` の空枠だけ。C1で実装）。
    97	
    98	### 3.9 server/tts（移設）
    99	- `tts/client.py`→`server/tts/client.py`: `_pcm_to_float32`等を **`from ...common.pcm import`** に変更、`AudioFormat` は `from ...common.audio_format import`。`TTSClient/stream/synthesize/_decode_*` は不変。
   100	- `tts/profile.py`→`server/tts/profile.py`: `AudioFormat` を common参照に。`load_profile` の探索基準（profiles_dir）は ServerConfig 由来に。
   101	
   102	### 3.10 client/audio（移設）
   103	- `audio/player.py`→`client/audio/player.py`: PCMヘルパーを **`from ...common.pcm import`**、`AudioFormat`不要なら除去。`StreamingPlayer/play/_device_samplerate` 不変（**lazy-start修正はC1**・R0では現状維持）。
   104	- `audio/devices.py`・`audio/routing.py`→`client/audio/`（不変）。
   105	
   106	## 4. pyproject 依存グループ＋エントリ
   107	- **トップレベル `dependencies` を common最小限に**：`numpy`, `pydantic`, （tomllibは標準）。
   108	- `[project.optional-dependencies]`:
   109	  - `client = ["PySide6>=...", "qasync>=...", "sounddevice>=...", "soxr>=...", "numpy>=...", "websockets>=...", "webrtcvad>=..."]`
   110	  - `server = ["torch>=2.11.0", "qwen-asr>=...", "fastapi>=...", "uvicorn>=...", "websockets>=...", "httpx>=...", "numpy>=...", "soxr>=..."]`
   111	  - 旧トップレベルの torch/qwen-asr/silero-vad/pyside6/... は各extraへ移動。`[[tool.uv.index]] cu128` は維持。
   112	- `[project.scripts]`:
   113	  - `text-to-shaberi-client = "text_to_shaberi.client.app:main"`
   114	  - `text-to-shaberi-server = "text_to_shaberi.server.app:main"`
   115	  - `text-to-shaberi = "text_to_shaberi.client.app:main"`（互換エイリアス）
   116	- `[dependency-groups] dev=["pytest>=9.0.3"]` は維持。
   117	
   118	## 5. 設定ファイル
   119	- 旧 `config.toml` を **`client.toml` と `server.toml` に分割**（旧 config.toml は削除）。値は §3.5/§3.7 のフィールドに割り当て（既存値を踏襲。VAD engine 既定は `webrtc`）。
   120	- `profiles/tts/gpt-sovits.toml` はそのまま（サーバが参照）。
   121	
   122	## 6. import境界（重要・codex#1,#2）
   123	- **`client/**` は `server/**`・`torch`・`qwen_asr` を import してはならない。** common と client 内のみ。
   124	- **`server/**` は `PySide6`・`sounddevice` を import してはならない。**
   125	- `common/**` は torch/PySide6/sounddevice 非依存（numpy/pydantic/標準のみ）。
   126	
   127	## 7. 受け入れ基準
   128	- [ ] `py_compile` 全モジュール成功。
   129	- [ ] `tests/` 成功（`test_pcm.py` を `common.pcm` import に更新＋下記境界テスト）。
   130	- [ ] **`uv sync --extra client`**（serverなし）環境で **`uv run text-to-shaberi-client --check` が torch/qwen_asr を import せず**成功（`--check`で設定/プロファイル/デバイス列挙）。
   131	- [ ] `uv run text-to-shaberi-server`（stub）が server.toml をロードし `/healthz` を返す。bind検証（外部bind+token空で起動拒否）。
   132	- [ ] client GUI が起動し、送信で「WS未接続(C1)」ログ（クラッシュなし）。
   133	- [ ] **import境界テスト** green。
   134	
   135	## 8. 変更/新規/削除
   136	- 新規: `common/{__init__,pcm,audio_format,protocol,config}.py`, `server/{__init__,config,app,ws_server}.py`, `server/tts/{__init__,client,profile}.py`, `server/stt/__init__.py`, `client/{__init__,config,app,ws_client}.py`, `client/audio/{__init__,devices,player,routing}.py`, `client/ui/{__init__,main_window}.py`, `server.toml`, `client.toml`, `tests/test_import_boundaries.py`
   137	- 移設元削除: `tts/`, `audio/`, `ui/`, `api/`, `config.py`, `app.py`, `__main__.py`(必要なら client へ), `config.toml`
   138	- 変更: `pyproject.toml`, `tests/test_pcm.py`(import更新), `src/text_to_shaberi/__init__.py`(最小化)
   139	
   140	## 9. テスト
   141	- `tests/test_pcm.py`: import元を `text_to_shaberi.common.pcm` に更新（テスト内容は不変）。
   142	- `tests/test_import_boundaries.py`（新規）: `import text_to_shaberi.client.app`（および client.audio.*, client.ui.*）後に `"torch" not in sys.modules` と `"qwen_asr" not in sys.modules` を assert。`text_to_shaberi.common.*` import 後に torch/PySide6 非ロードを assert。
   143	- 手動: `uv sync --extra client && uv run text-to-shaberi-client --check`。
   144	
   145	## 10. 留意点
   146	- 移設は**ロジック不変**（等価リファクタ）。挙動変更（lazy-start等）はR0で入れない（C1）。
   147	- 相対import段数に注意（`server/tts/client.py` から common は `from ...common.pcm import`）。
   148	- `__init__.py` の再エクスポートで誤って client→server を繋げない。
   149	- `webrtcvad` は R0 では client extra に入れるだけ（使用はC2）。Windowsホイール有無は別途確認。

codex
**レビュー指摘**

1. **高: `client --check` が server/TTS profile に触る設計のまま**
   現行 [app.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:14>) はトップレベルで `load_profile` を import し、`run_check()` で `profiles/tts` を読む。R0 計画では client は server 配下を import 禁止なので、§7 の「`--check`で設定/プロファイル/デバイス列挙」は境界違反を誘発します。
   修正提案: R0 の client `--check` は `client.toml`、WS address/token、output devices、IME 設定のみ確認に限定。プロファイル確認は server `--check` または `/healthz` 以降へ移す。

2. **高: `MainWindow` が旧 `AppConfig` 形状に依存しており GUI stub が壊れる**
   現行 [main_window.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/ui/main_window.py:69>) は `config.active_tts_profile` と `config.stt.backend` を参照するが、§3.7 の `ClientConfig` には存在しません。計画の「MainWindow 生成はそのまま」はクラッシュします。
   修正提案: R0 で UI 初期ログを `server.address` / `profile.active_id or server default` へ変更することを明記。`MainWindow` が client config だけで成立するようにする。

3. **高: デフォルト設定パスの段数が未指定**
   現行 `DEFAULT_CONFIG` は [app.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:18>) から `parents[2] / "config.toml"`。移設後 `client/app.py`、`server/app.py` は階層が1段深くなるため、同じ式だと `src/client.toml` 側を見に行く恐れがあります。
   修正提案: `client/app.py` と `server/app.py` の既定パスを明記する。例: `Path(__file__).resolve().parents[3] / "client.toml"` / `"server.toml"`。

4. **高: `__main__.py` の扱いが曖昧で `python -m text_to_shaberi` が壊れる**
   §8 は `__main__.py` を「削除(必要なら client へ)」としているが、現行 [__main__.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/__main__.py:1>) は `python -m` の入口です。削除すると互換性が落ちます。
   修正提案: R0 では `src/text_to_shaberi/__main__.py` を残し、`from .client.app import main` に変更する、と明記。`__init__.py` は再エクスポートしない方針でよいです。

5. **高: import 境界テストが `sys.modules` だけでは不十分**
   §9 の `sys.modules` チェックは「実際に import されたか」しか見ません。`client` 内に `from text_to_shaberi.server...` が遅延 import で残っていても、テスト経路で実行されなければ検出できません。
   修正提案: AST ベースの静的テストを追加。`client/**` の `Import` / `ImportFrom` から `text_to_shaberi.server`, `torch`, `qwen_asr`, `silero_vad` を禁止し、`server/**` から `PySide6`, `sounddevice` を禁止する。`sys.modules` テストは補助として残す。

6. **中: 依存 extras の検証手順が server/client 両方で不足**
   §7 は client extra だけ明確だが、server 起動の前に `uv sync --extra server` が必要です。また `uv sync --extra client` は既存 `.venv` に server 依存が残っていると検証になりません。
   修正提案: 受け入れ基準に「新規 venv または `uv sync --extra client --no-dev` 相当のクリーン環境で `python -c 'import importlib.util; assert importlib.util.find_spec("torch") is None'`」を追加。server 側は `uv sync --extra server` を明記。

7. **中: `uv run text-to-shaberi-server` の受け入れ基準がブロッキングで曖昧**
   §7 の「`uv run text-to-shaberi-server` が `/healthz` を返す」は、そのコマンドが常駐するため GLM が検証方法で迷います。
   修正提案: `server.ws_server:create_app()` を TestClient で叩く自動テスト、または手動検証を `uv run text-to-shaberi-server -c server.toml` を別プロセス起動して `curl http://127.0.0.1:8765/healthz` と明記。

8. **中: `tts/profile.py` の全要素移行が表に明示されていない**
   `render_value`, `TTSProfile.render_body`, `load_profile` は移設されるはずだが、§2/§3.9 の説明は `TTSProfile/load_profile` 中心です。
   修正提案: §3.9 に「`render_value` と `TTSProfile.render_body` も不変で移設」を追加。`server/tts/__init__.py` の再エクスポート範囲も明記。

9. **中: protocol schema が ARCH より少し欠けている**
   ARCH の `ready` 例には `server_time` があるが、§3.3 の `Ready` にありません。また `format`, `profiles`, `limits`, `features`, `caps` がすべて裸の `dict/list` で、R0 後の S1/C1 実装時に型が揺れます。
   修正提案: R0 最低限として `Literal[...]` の `type`、`AudioFormatPayload`、`ProfileInfo{id,name}`、`Ready.server_time: float|None = None` を入れる。過剰な状態機械ロジックは入れない。

10. **中: bind 検証が `"127.0.0.1"` 固定だと過剰/過少**
   §3.5 の `host != "127.0.0.1"` は `localhost`, `::1` を拒否し、逆に名前解決系の扱いが曖昧です。
   修正提案: `ipaddress` で loopback 判定。`0.0.0.0`, `::`, LAN/private/public は token 必須。解決不能な hostname は安全側で token 必須。

11. **中: R0 の「動作等価性」方針が文書内で矛盾**
   §1 は「動作の等価性は問わず」、§10 は「移設はロジック不変（等価リファクタ）」です。実際には TTS 直叩きを stub にするので GUI 発話動作は変わります。
   修正提案: 「低レベルロジックは不変。ただし client 発話は意図的に stub 化し、既存 TTS 実行動作は S1/C1 まで一時的に失われる」と明文化。

12. **低: `AudioBuffer` を common に置くのは許容だが理由を補うべき**
   `AudioBuffer` は現在 TTS decode 結果の型で、R0 client stub 後は主に server 側利用です。ただし `numpy` が common 依存に入るなら境界上は問題ありません。
   修正提案: §3.1 に「PCM decode の戻り値型として server TTS と将来 client decode/play test で共有するため common」と理由を追記。不要なら `server/tts` に残す選択肢もあるが、計画全体では common 配置で問題なし。

13. **低: VAD 既定を webrtc に変えるのは設計上妥当だが R0 では挙動影響なしにする**
   client extra に `webrtcvad` を入れるだけならよいです。ただし `ClientConfig` ロードで import しないこと、R0 で実処理に使わないことを明記した方が安全です。
   修正提案: §3.7/§10 に「設定値のみ。`import webrtcvad` は C2 まで行わない」を追加。

**総評**

計画の大枠、特に `common.pcm` / `common.audio_format` 抽出、`client` から TTS/server を切る方向、cu128 index 維持は妥当です。一方で、現行 `app.py` と `MainWindow` が旧 monolith config/profile に強く依存しているため、GLM がそのまま移設すると client-only 起動条件を満たせない可能性が高いです。R0 は「機械的リファクタ」ではなく「client 発話を stub にして設定/UI 依存を切る」小さな設計変更を含む、と明示すべきです。

**GLM委託前に必ず反映すべき TOP3**

1. `client --check` と `MainWindow` を `ClientConfig` のみで成立させる修正指示を追加する。
2. import 境界テストを `sys.modules` だけでなく AST 静的検査にする。
3. `__main__.py`、既定 config path、server/client extras の検証コマンドを具体化する。
hook: Stop
hook: Stop Completed
tokens used
44,005
**レビュー指摘**

1. **高: `client --check` が server/TTS profile に触る設計のまま**
   現行 [app.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:14>) はトップレベルで `load_profile` を import し、`run_check()` で `profiles/tts` を読む。R0 計画では client は server 配下を import 禁止なので、§7 の「`--check`で設定/プロファイル/デバイス列挙」は境界違反を誘発します。
   修正提案: R0 の client `--check` は `client.toml`、WS address/token、output devices、IME 設定のみ確認に限定。プロファイル確認は server `--check` または `/healthz` 以降へ移す。

2. **高: `MainWindow` が旧 `AppConfig` 形状に依存しており GUI stub が壊れる**
   現行 [main_window.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/ui/main_window.py:69>) は `config.active_tts_profile` と `config.stt.backend` を参照するが、§3.7 の `ClientConfig` には存在しません。計画の「MainWindow 生成はそのまま」はクラッシュします。
   修正提案: R0 で UI 初期ログを `server.address` / `profile.active_id or server default` へ変更することを明記。`MainWindow` が client config だけで成立するようにする。

3. **高: デフォルト設定パスの段数が未指定**
   現行 `DEFAULT_CONFIG` は [app.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:18>) から `parents[2] / "config.toml"`。移設後 `client/app.py`、`server/app.py` は階層が1段深くなるため、同じ式だと `src/client.toml` 側を見に行く恐れがあります。
   修正提案: `client/app.py` と `server/app.py` の既定パスを明記する。例: `Path(__file__).resolve().parents[3] / "client.toml"` / `"server.toml"`。

4. **高: `__main__.py` の扱いが曖昧で `python -m text_to_shaberi` が壊れる**
   §8 は `__main__.py` を「削除(必要なら client へ)」としているが、現行 [__main__.py](</mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/__main__.py:1>) は `python -m` の入口です。削除すると互換性が落ちます。
   修正提案: R0 では `src/text_to_shaberi/__main__.py` を残し、`from .client.app import main` に変更する、と明記。`__init__.py` は再エクスポートしない方針でよいです。

5. **高: import 境界テストが `sys.modules` だけでは不十分**
   §9 の `sys.modules` チェックは「実際に import されたか」しか見ません。`client` 内に `from text_to_shaberi.server...` が遅延 import で残っていても、テスト経路で実行されなければ検出できません。
   修正提案: AST ベースの静的テストを追加。`client/**` の `Import` / `ImportFrom` から `text_to_shaberi.server`, `torch`, `qwen_asr`, `silero_vad` を禁止し、`server/**` から `PySide6`, `sounddevice` を禁止する。`sys.modules` テストは補助として残す。

6. **中: 依存 extras の検証手順が server/client 両方で不足**
   §7 は client extra だけ明確だが、server 起動の前に `uv sync --extra server` が必要です。また `uv sync --extra client` は既存 `.venv` に server 依存が残っていると検証になりません。
   修正提案: 受け入れ基準に「新規 venv または `uv sync --extra client --no-dev` 相当のクリーン環境で `python -c 'import importlib.util; assert importlib.util.find_spec("torch") is None'`」を追加。server 側は `uv sync --extra server` を明記。

7. **中: `uv run text-to-shaberi-server` の受け入れ基準がブロッキングで曖昧**
   §7 の「`uv run text-to-shaberi-server` が `/healthz` を返す」は、そのコマンドが常駐するため GLM が検証方法で迷います。
   修正提案: `server.ws_server:create_app()` を TestClient で叩く自動テスト、または手動検証を `uv run text-to-shaberi-server -c server.toml` を別プロセス起動して `curl http://127.0.0.1:8765/healthz` と明記。

8. **中: `tts/profile.py` の全要素移行が表に明示されていない**
   `render_value`, `TTSProfile.render_body`, `load_profile` は移設されるはずだが、§2/§3.9 の説明は `TTSProfile/load_profile` 中心です。
   修正提案: §3.9 に「`render_value` と `TTSProfile.render_body` も不変で移設」を追加。`server/tts/__init__.py` の再エクスポート範囲も明記。

9. **中: protocol schema が ARCH より少し欠けている**
   ARCH の `ready` 例には `server_time` があるが、§3.3 の `Ready` にありません。また `format`, `profiles`, `limits`, `features`, `caps` がすべて裸の `dict/list` で、R0 後の S1/C1 実装時に型が揺れます。
   修正提案: R0 最低限として `Literal[...]` の `type`、`AudioFormatPayload`、`ProfileInfo{id,name}`、`Ready.server_time: float|None = None` を入れる。過剰な状態機械ロジックは入れない。

10. **中: bind 検証が `"127.0.0.1"` 固定だと過剰/過少**
   §3.5 の `host != "127.0.0.1"` は `localhost`, `::1` を拒否し、逆に名前解決系の扱いが曖昧です。
   修正提案: `ipaddress` で loopback 判定。`0.0.0.0`, `::`, LAN/private/public は token 必須。解決不能な hostname は安全側で token 必須。

11. **中: R0 の「動作等価性」方針が文書内で矛盾**
   §1 は「動作の等価性は問わず」、§10 は「移設はロジック不変（等価リファクタ）」です。実際には TTS 直叩きを stub にするので GUI 発話動作は変わります。
   修正提案: 「低レベルロジックは不変。ただし client 発話は意図的に stub 化し、既存 TTS 実行動作は S1/C1 まで一時的に失われる」と明文化。

12. **低: `AudioBuffer` を common に置くのは許容だが理由を補うべき**
   `AudioBuffer` は現在 TTS decode 結果の型で、R0 client stub 後は主に server 側利用です。ただし `numpy` が common 依存に入るなら境界上は問題ありません。
   修正提案: §3.1 に「PCM decode の戻り値型として server TTS と将来 client decode/play test で共有するため common」と理由を追記。不要なら `server/tts` に残す選択肢もあるが、計画全体では common 配置で問題なし。

13. **低: VAD 既定を webrtc に変えるのは設計上妥当だが R0 では挙動影響なしにする**
   client extra に `webrtcvad` を入れるだけならよいです。ただし `ClientConfig` ロードで import しないこと、R0 で実処理に使わないことを明記した方が安全です。
   修正提案: §3.7/§10 に「設定値のみ。`import webrtcvad` は C2 まで行わない」を追加。

**総評**

計画の大枠、特に `common.pcm` / `common.audio_format` 抽出、`client` から TTS/server を切る方向、cu128 index 維持は妥当です。一方で、現行 `app.py` と `MainWindow` が旧 monolith config/profile に強く依存しているため、GLM がそのまま移設すると client-only 起動条件を満たせない可能性が高いです。R0 は「機械的リファクタ」ではなく「client 発話を stub にして設定/UI 依存を切る」小さな設計変更を含む、と明示すべきです。

**GLM委託前に必ず反映すべき TOP3**

1. `client --check` と `MainWindow` を `ClientConfig` のみで成立させる修正指示を追加する。
2. import 境界テストを `sys.modules` だけでなく AST 静的検査にする。
3. `__main__.py`、既定 config path、server/client extras の検証コマンドを具体化する。
