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
