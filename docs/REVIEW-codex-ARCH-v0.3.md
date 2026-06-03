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
session id: 019e8b32-3ced-7fe1-a4b0-83832bd843bf
--------
user
あなたはシニアソフトウェアアーキテクトです。docs/ARCH-v0.3-client-server.md を批判的にレビューしてください。これは既存のモノリシック実装(docs/PLAN.md v0.2)を「Windowsクライアント(GUI/マイク/再生) ↔ WebSocket ↔ ヘッドレスGPUサーバ(STT Qwen3-ASR / TTS GPT-SoVITS)」へ分離する改訂計画です。この後この計画をもとに段階実装します(R0→S1→C1→S2→C2→X1→X2)。

参照: docs/PLAN.md, docs/IMPL-M2.md, docs/CODE-REVIEW-M2.md, src/text_to_shaberi/ 既存実装(tts/client.py, audio/player.py, audio/routing.py, ui/main_window.py, app.py, config.py)

観点:
1. クライアント-サーバ分離の妥当性と、既存モノリシック実装(M0-M2)の移行マップ(§8)の現実性。捨てる/壊れる部分は本当に無いか
2. WebSocketプロトコル(§4)の設計：制御JSON＋バイナリ音声、id関連付け(単一ストリーム前提)、speak/stt/stop、ハンドシェイク。多重化やバックプレッシャ、切断/再接続、順序保証の抜け
3. 音声コーデック自動(PCM/Opus)戦略(§5)とネゴシエーションの妥当性、Windowsでのopus依存
4. 依存グループ分離(§3)：Windowsクライアントがtorch不要で成立するか、共有protocolの置き場
5. レイテンシ予算(§7)：ネットワーク込みでSLO(TTFA≤400ms)が現実的か。STT→自動TTSのサーバ内連結フロー(§10-6)
6. セキュリティ(§6)：LAN/遠隔、token、wss/TLS、bind、Origin
7. クライアント設定UI(歯車/設定ダイアログ・client.toml永続化)の設計妥当性
8. 改訂マイルストーン(§9)の順序・粒度・抜け、受け入れ基準(§11)
9. Windows実機リスク(WASAPI/sounddevice/Silero VAD/VB-CABLE)
10. 見落とし・曖昧さ(段階実装で迷う点)

出力はファイル編集せず、Markdownのレビューのみ。各指摘に重要度(高/中/低)と具体的修正提案を付け、最後に総評と「実装(R0)着手前に計画へ必ず反映すべきTOP3」を挙げてください。
hook: UserPromptSubmit
hook: UserPromptSubmit
hook: UserPromptSubmit Completed
hook: UserPromptSubmit Completed
codex
対象ドキュメントと既存実装を読み比べて、計画上の破綻や段階実装で詰まりそうな箇所を中心に確認します。まずは参照ファイルの構成と該当セクションを把握します。
exec
/bin/bash -lc "sed -n '1,260p' docs/ARCH-v0.3-client-server.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# アーキテクチャ改訂 v0.3: クライアント–サーバ分離

> 本書は `docs/PLAN.md` v0.2（モノリシック前提）を**改訂**する。ヒアリング(2026-06-03)で
> 「フロント=Windows(マイク)／処理=ヘッドレスGPUサーバ」という分離構成が確定したため。
> フロー: 本書を codex レビュー → 反映 → 各マイルストーンの実装計画 → GLM 委託 → 検証。

## 1. 変更点（モノリシック → 分離）
- 旧: 1プロセスで mic→STT→TTS→再生（GPUマシン上）。
- 新: **Windowsクライアント（GUI/マイク/VAD/再生）** ↔ **WebSocket** ↔ **ヘッドレスGPUサーバ（STT/TTS）**。
- 既存実装(M0足場/M1 TTSコア/M2 StreamingPlayer/IME)は**捨てず配置換えして再利用**（§8 移行マップ）。

## 2. 全体アーキテクチャ
```
 Windows クライアント                              Linux GPU サーバ (headless)
┌───────────────────────────┐   WebSocket   ┌──────────────────────────────┐
│ PySide6 GUI                │◄─ws(s)://────►│ WS backend (FastAPI/websockets)│
│ マイク取得 + VAD(Silero)    │  制御=JSON     │  ├ Orchestrator(セッション/割込) │
│ テキスト入力 / API入力(LLM) │  音声=binary   │  ├ STT: Qwen3-ASR (GPU常駐)      │
│ StreamingPlayer 再生        │  (PCM/Opus自動)│  ├ TTS: GPT-SoVITSクライアント   │
│ 出力デバイス選択(VB-CABLE等) │                │  │      (tomlプロファイル)        │
└───────────────────────────┘                │  └ Codec(PCM/Opus) + 認証/TLS   │
                                              └───────────────┬──────────────┘
                                                GPT-SoVITS :9880 (別プロセス・既存)
```

## 3. リポジトリ / 依存の再編
```
src/text_to_shaberi/
├── common/    # 共有: protocol(WSメッセージ&スキーマ) / audio_format / codec(PCM,Opus) / 設定共通
├── server/    # バックエンド: ws_server / orchestrator / stt(Qwen) / tts(GPT-SoVITSクライアント+profile)
├── client/    # フロント: ui(PySide6) / mic(sounddevice+VAD) / player(StreamingPlayer) / devices / ws_client
└── (entry)    # text-to-shaberi-server  /  text-to-shaberi-client
```
- **依存グループ**（`pyproject` optional-dependencies）:
  - `server`: torch(cu128), qwen-asr, fastapi, uvicorn, websockets, httpx, numpy, (soxr)
  - `client`: PySide6, qasync, sounddevice, soxr, numpy, websockets, (opus: `opuslib`/`PyOgg`)
  - `common`: numpy, pydantic, tomllib(標準)
- **Windowsクライアントは client+common のみインストール**（torch/qwen 不要）。サーバは server+common。
- 設定: `server.toml` / `client.toml` に分割（共通ローダ）。TTSプロファイルはサーバ側 `profiles/tts/*.toml`。
- **クライアントUI方針**: メイン画面は**シンプル維持**（入力・送信・停止・出力先・ログ）。増えていく設定は**歯車(設定)ボタン → 設定ダイアログ**に集約する（ハンバーガーでも可）。設定ダイアログの項目: **サーバWSアドレス / 認証トークン / 接続(LAN・遠隔) / コーデック(自動/PCM/Opus) / 出力デバイス / TTSプロファイル選択 / 将来項目**。設定は `client.toml` に永続化し、起動時ロード・変更時保存。機能トグル（例: 自動TTS, マイクON/OFF）も同ダイアログに。

## 4. WebSocket プロトコル（v1）
- 接続: `ws(s)://host:port/ws`。認証は接続時 `Authorization: Bearer <token>`（または最初の `hello` に同梱）。
- 制御は**テキスト(JSON)フレーム**、音声は**バイナリフレーム**。M-scopeは方向ごとに**同時1ストリーム**前提（多重化はキュー導入時に拡張）。
- ハンドシェイク:
  - C→S `{"type":"hello","caps":{"codecs":["pcm_s16le","opus"]}}`
  - S→C `{"type":"ready","codec":"pcm_s16le","tts":{"sample_rate":32000,"channels":1}}`（環境/設定でcodec決定）
- 発話(テキスト→音声):
  - C→S `{"type":"speak","id":"u1","text":"…","profile":"gpt-sovits","interrupt":false}`
  - S→C `{"type":"audio_begin","id":"u1","format":{"codec":"pcm_s16le","sample_rate":32000,"channels":1}}`
  - S→C `<binary audio chunk> …`（直近 audio_begin に属する）
  - S→C `{"type":"audio_end","id":"u1"}`
- 音声認識(マイク→テキスト):
  - C→S `{"type":"stt_begin","id":"u2","format":{"codec":"pcm_s16le","sample_rate":16000,"channels":1}}`
  - C→S `<binary audio chunk> …`（発話セグメント）
  - C→S `{"type":"stt_end","id":"u2"}`
  - S→C `{"type":"transcript","id":"u2","text":"…","final":true}`
  - サーバ設定で **自動TTS** なら続けて audio_begin/…/audio_end も送る。
- 制御/エラー:
  - C→S `{"type":"stop","id":"u1"}` → サーバはTTS/STT生成をキャンセル、`{"type":"stopped","id":"u1"}`。
  - S→C `{"type":"error","id":…,"message":"…"}`
- スキーマは `common/protocol.py`（pydantic）で型定義し client/server で共有。

## 5. 音声コーデック戦略（自動 PCM/Opus）
- `common/codec.py`: `encode(frames,fmt)->bytes` / `decode(bytes,fmt)->frames`。`pcm_s16le`(無変換) と `opus`(48k/20msフレーム) を実装。
- ネゴシエーション: サーバ設定 `transport="auto"|"pcm"|"opus"`。`auto` は接続元が loopback/LAN(プライベートIP) なら PCM、それ以外 Opus。`hello.caps` と突き合わせて `ready.codec` 決定。
- TTS出力(32kHz)→Opus(48k)変換は soxr。受信側で逆変換。**rawは AudioFormat を audio_begin で明示**（§PLAN §8 の原則を踏襲）。

## 6. セキュリティ
- LAN: `token` 任意（既定は共有トークン1個）。遠隔: **token必須 ＋ wss/TLS必須**。
- サーバは既定 `0.0.0.0` ではなく設定で bind を明示。CORS/Origin チェック。レート制限は後続。

## 7. レイテンシ予算（ネットワーク込みで再評価）
- LAN: WS往復 ~1–5ms。**GUI/API TTFA = 送信 + net + サーバ(GPT-SoVITS first-chunk 148–270ms 実測) + net + 復号/再生** ≈ ローカル+5–15ms → **目標 p50≤400ms 維持可**。
- マイク往復 = mic+VAD(端末) + net + STT(0.29s) + TTS first-chunk + net + 再生 ≈ LANで+10–20ms。
- Opus enc/dec ~数ms。遠隔は RTT 次第（受け入れ基準は LAN を主、遠隔は best-effort）。

## 8. 既存コードの移行マップ（M0–M2 の配置換え）
| 既存 | 移行先 | 備考 |
|---|---|---|
| `tts/client.py`(TTSClient/stream/decode) | `server/tts/` | サーバ内でGPT-SoVITSをstreaming |
| `tts/profile.py` | `server/tts/`（一部fmtは`common`） | tomlプロファイル |
| `audio/player.py`(StreamingPlayer) | `client/audio/` | **lazy-start修正込み**で再利用 |
| `audio/devices.py` | `client/audio/` | Windowsは VB-CABLE をデバイス選択 |
| `audio/routing.py`(PipeWire) | `client/audio/`(Linux/dev用) | Windows本番では未使用 |
| `ui/main_window.py` | `client/ui/` | WS送信に結線 |
| `api/server.py`(FastAPI) | `server/ws_server` | WSバックエンドへ昇格(REST /speak も残置可) |
| `config.py` | `common`＋server/client個別 | server.toml/client.toml |
| `app.py` | `client`(run_gui)＋`server`(run_server) | エントリ2つ |

## 9. 改訂マイルストーン
- **R0 再編**: common/server/client へ分割、依存グループ、`common/protocol.py`(pydantic)、既存コード移設、両エントリの空起動。
- **S1 サーバTTS-WS**: `speak`→GPT-SoVITS streaming→PCM音声フレーム送信＋`stop`キャンセル。
- **C1 クライアント再生**: GUI→WS `speak`→受信→**StreamingPlayer(lazy-start修正)** 再生。LAN/PCMで「テキスト→声」疎通＋TTFA計測。＋**歯車ボタンの設定ダイアログ最小版**（サーバWSアドレス/トークンを設定→`client.toml`保存、再接続）。
- **S2 サーバSTT-WS**: 音声受信→Qwen→`transcript`（＋自動TTSオプション）。
- **C2 マイク+VAD**: 端末マイク常時録音・Silero VAD・発話セグメント→WS→transcript/音声往復（マイク往復完成）。
- **X1 コーデック/セキュリティ**: PCM/Opus自動＋token認証＋wss/TLS（遠隔対応）。
- **X2 仕上げ**: ネット込みレイテンシ計測・WS再接続/バックプレッシャ・設定UI・Windows実機(VB-CABLE)・エラー処理。

## 10. リスク・未決
1. **Windows音声(WASAPI)** での sounddevice/StreamingPlayer 挙動（latency, lazy-start, デバイス列挙, VB-CABLE）→ 実機検証。
2. **Opusの依存**（`opuslib`/`PyOgg` のWindowsホイール入手性）→ 無ければ PCM 限定で先行。
3. **WSバイナリのid関連付け**（M-scopeは方向ごと単一ストリーム前提。多重化はキュー導入で）。
4. **Silero VAD のWindows動作**（torch依存の有無。webrtcvadフォールバック検討）。
5. **再接続/切断時**のセッション復帰・バックプレッシャ（送信過多で詰まらせない）。
6. STT結果→自動TTSの**サーバ内連結**か、クライアント往復かのフロー確定（設定で選べる形を推奨）。

## 11. 受け入れ基準（疎通MVP）
- [ ] サーバ(headless)起動＋クライアント(GUI)が ws 接続・`hello/ready` 成立。
- [ ] テキスト送信→サーバTTS→クライアント再生（**先頭欠け無し**）。**TTFA(ネット込み, LAN) ≤ 400ms** をログ表示。
- [ ] マイク発話→サーバSTT→transcript表示→（自動TTSなら）再生。
- [ ] `stop` で音停止＋サーバ生成キャンセル。
- [ ] LAN/PCM と Opus 双方で疎通。token認証（遠隔は wss）。
- [ ] **設定ダイアログ(歯車)** からサーバWSアドレス/トークン等を変更・`client.toml`保存でき、再接続に反映。
- [ ] サーバ/クライアント各失敗（接続断・サーバ不達・OOM）でクラッシュせず復帰。

## 12. PLAN.md との関係
- §3 SLO・§7 tomlプロファイル・§5 STT・§8 音声I/O原則は**維持**（配置がサーバ/クライアントに分かれるだけ）。
- §6 並行設計はクライアント(qasync+mic/再生スレッド)とサーバ(asyncio WS + STTワーカースレッド)に分割。
- 旧 M3–M6 は本書 §9 の S1/C1/S2/C2/X1/X2 に再編。

exec
/bin/bash -lc "sed -n '1,260p' docs/PLAN.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# Text-to-shaberi 計画書（v0.2）

> **更新履歴**
> - v0.1: 初版ドラフト（ブレスト＋外部仕様調査）
> - **v0.2: codex(gpt-5.5) レビューを反映**（→ `docs/REVIEW-codex.md`）。主な変更＝(a)用途別レイテンシSLOを定義 (b)TTSプロファイルschemaを大幅拡張 (c)性能PoC(Spike)をUI実装より前に配置 (d)キュー/割り込み(Utterance)モデルを明文化 (e)音声フォーマット/リサンプリングを必須化 (f)並行境界・FastAPIライフサイクル・受け入れ基準を強化。

---

## 1. 目的・概要

複数の入力ソースから得たテキストを、任意の TTS-API サーバへ送信して音声を返す**低遅延デスクトップアプリ**。

- 入力は **3 系統**：①マイク音声（常時録音 → STT でテキスト化）／②GUI 手入力テキスト／③API 入力（LLM からの送信を想定）
- 出力は合成音声を **既定デバイス or 仮想オーディオデバイス**へ再生（GUI で切替）
- TTS サーバごとに異なるリクエストボディ/レスポンス形式を **複数の toml で設定**できる
- **Python (uv) + PySide6** デスクトップアプリ。**レイテンシ重視**。

## 2. 想定ユースケース（ヒアリング確定）

1. **AITuber / 配信**：LLM 出力やマイク発話を合成音声で読み上げ、配信に流す（仮想オーディオ出力前提）
2. **ボイスチェンジャー的用途**：自分のマイク音声を STT→TTS で別の合成音声に変換（往復レイテンシが最重要・最難）
3. **読み上げ・アクセシビリティ**：入力テキスト/発話を合成音声で読み上げ（既定スピーカー）

→ 用途が広いため、**低遅延**と**出力先切替**の両立が必須要件。

## 3. レイテンシ目標（SLO）★v0.2 追加

用途別に合否ラインを数値化する。**下記は暫定値（PoC=Spikeで実測し確定）**。受け入れ基準（§12）と対応。

| 指標 | 暫定目標 | 備考 |
|------|----------|------|
| GUI/API入力 TTFA（送信→最初の音） | p50 ≤ 400ms / p95 ≤ 800ms | TTSストリーミング前提 |
| マイク往復（発話終端→最初の音） | p50 ≤ 1.2s / p95 ≤ 2.0s | **疑似streaming**前提。真リアルタイムは将来課題 |
| 発話終端待ち（VAD無音判定） | 400–600ms（調整可） | 体感応答と切り過ぎのトレードオフ |
| 割り込み→無音化 | ≤ 150ms | 再生停止＋必要ならTTS/HTTPキャンセル |
| 再生 underrun | 0 回/分（目標） | バッファ設計で担保 |

> ⚠ ボイスチェンジャー用途（マイク往復）は VAD終端待ち+STT+TTS の合計で物理的に下限がある。**「実用域」を受け入れ基準に入れる前に Spike で実測ゲートを通す**（§10 のリスク#1）。
>
> **Spike実測(2026-06-03)・全PASS**：①TTS first-chunk＝**270ms**（`streaming_mode=3`,raw）→ GUI/API TTFA目標(p50≤400ms)**達成**（非streamingは2.3sでTTFA不可→streaming必須）。②Qwen STT＝**0.29s**（sdpa）。③同時常駐VRAM＝約7-9GB≪24GB。GPT-SoVITS出力＝pcm_s16le/32000/mono。
> **マイク往復の見積り＝VAD終端待ち(400-600ms)＋STT0.29s＋TTS0.27s ≈ 1.0-1.2s**（目標p50≤1.2s内・タイトだが達成可）。

## 4. 技術選定（ヒアリング＋調査確定）

| 領域 | 採用 | 備考 |
|------|------|------|
| STT | **Qwen3-ASR**（プライマリ＝ローカル `Qwen/Qwen3-ASR-1.7B`） | 日本語対応・OSS。`STTBackend` 抽象でクラウド(DashScope)も差替可。VRAMは**検証ゲート**(§5.1) |
| TTS | **GPT-SoVITS**（`api_v2`, `POST /tts` :9880） | toml駆動の汎用HTTPドライバで叩く。`streaming_mode` の型はバージョン差を吸収 |
| GUI | **PySide6** | |
| 非同期 | **qasync**（asyncio ⇆ Qt 統合）＋ STT 用ワーカースレッド | UIを止めない。スレッド境界は§6で明文化 |
| 音声 I/O | **sounddevice (PortAudio)** | デバイス列挙・入出力 |
| リサンプリング | **soxr**（`soxr` / `python-soxr`） | TTS出力rate → 出力デバイスrate 変換（§8） |
| VAD | **Silero VAD**（軽量フォールバックに webrtcvad） | 無音検知＝発話区間切り出し |
| ノイズ抑制 | **段階式**：MVPはゲイン/ノイズゲート等の軽量前処理、RNNoise等は**プラグイン(任意)** | 機構は常設・既定OFF。ON時の追加遅延をメトリクス化（§10 リスク#5） |
| TTS への HTTP | **httpx**（async, keep-alive, キャンセル可能） | 接続再利用＋割り込み時リクエストキャンセル |
| API 入力サーバ | **FastAPI + uvicorn**（アプリ内蔵・別スレッド） | ライフサイクル/認証を§7で定義 |
| パッケージ | **uv** / `pyproject.toml` | |

### 5. STT バックエンド

#### 5.1 ローカル `Qwen3-ASR-1.7B`（プライマリ）
- 長所：ネットワーク遅延ゼロ・課金なし・プライバシー・日本語明示対応・オフライン
- 実装：`pip install qwen-asr`、`Qwen3ASRModel.from_pretrained(..., dtype=bfloat16, device_map="cuda:0")`、`.transcribe(audio=<np/wav>, language="Japanese")`
- **言語マッピング固定**：config の `ja` ⇔ API の `language="Japanese"` の対応を実装前に表で固定。
- **VRAM は「解決」でなく「検証ゲート」**（codex#7）：
  - RTX 3090 Ti (24GB) を前提とするが、GPT-SoVITS のモデル構成・vLLM `gpu_memory_utilization`・FlashAttention有無で実使用量は変動。
  - 対策：**起動時に VRAM を実測**、モデルロード順を固定、**OOM時フォールバック**（クラウドSTT切替 / GPT-SoVITS再ロード / 縮退）を設計。`Spike-C` で同時常駐の実測を取る。
  - **実測(2026-06-03) Spike-C PASS**：GPT-SoVITSサーバ ≈1.6GB ＋ Qwen3-ASR(bf16) ≈4.7GB ＝ 同時でも約7〜9GB ≪ 24GB。同時常駐に問題なし。OOMフォールバックは保険として残置。

#### 5.2 STT のストリーミング粒度（codex#2 を反映）
- **MVP = 疑似ストリーミング**（正直に定義）：VADで区切った発話セグメント単位で `transcribe`。マイク経路では「最初の音まで」の短縮効果は限定的＝SLO（§3）もこの前提。
- **将来ルート（別ルートとして明記）**：`音声チャンク → partial STT → 文/句の暫定確定 → 即TTS`。真の低遅延マイク用途は **vLLM streaming** か別ASRの検証対象に分離（`Spike-B` で評価）。

#### 5.3 クラウド DashScope `qwen3-asr-flash-realtime`（代替）
- WebSocketリアルタイム・低遅延、`DASHSCOPE_API_KEY` 必要。
- **注意**：リアルタイム版の公式Docに日本語が明記されていない → 採用時は日本語精度を事前検証。

## 6. 全体アーキテクチャと並行設計

```
┌──────────────── 入力層 ────────────────┐
│ ① Mic (sounddevice InputStream 16kHz/mono, 常時)        │
│     → RingBuffer → [軽量前処理/任意Denoise] → VAD(Silero)│
│     → 発話セグメント                                     │
│ ② GUI テキスト入力                                       │
│ ③ API 入力 (FastAPI: POST /speak / WS /stream)          │
└───────────────┬─────────────────────────┘
                │ ①は STT 経由でテキスト化
                ▼
        ┌─ STT 層 (Qwen3-ASR, ワーカースレッド) ─┐  ← ①のみ通過
        └───────────┬───────────────────────┘
                    ▼
   ┌── テキスト処理層 ──┐
   │ 正規化 / 文分割(句読点) → Utterance 生成 │
   │ → 優先度付きキュー（§6.2）              │
   └──────────┬─────────┘
              ▼
   ┌── TTS 層 (toml 駆動 汎用 HTTP ドライバ) ──┐
   │ Profile(toml) で request 構築 → httpx     │
   │ → response.mode に従い音声抽出            │
   │ → streaming で first-chunk を即再生        │
   └──────────────┬───────────────────────┘
                  ▼
   ┌── 出力層 (sounddevice OutputStream) ──┐
   │ AudioFormat検証 → soxrでrate変換 → 再生   │
   │ デバイス切替時は drain/stop/reopen        │
   └──────────────────────────────────────┘
```

### 6.1 並行モデルの境界ルール（codex#6 を反映）
- **メインスレッド**：Qt UI ＋ asyncio(qasync) イベントループ。**UI更新は必ず Qt signal 経由**。
- **マイク callback（PortAudioスレッド）**：**確保・ログ・await・重い処理を禁止**。リングバッファへの**コピーのみ**。
- **STTワーカースレッド**：GPU推論（ブロッキング）専用。入出力は **thread-safe queue**。
- **asyncio タスク**：TTS(httpx)・API。**スレッド⇔asyncio境界は `asyncio.Queue` / `run_in_executor` / `loop.call_soon_threadsafe` に限定**。
- **シャットダウン/例外**：各ワーカーに停止トークン、例外はイベントバス経由でUIに集約表示。終了時 graceful shutdown（録音停止→キュー破棄→HTTPキャンセル→ストリームclose）。

### 6.2 テキストキューと割り込み（Utterance）★codex#5 を反映
- **`Utterance`** モデル：`id / source(mic|gui|api) / text / profile / priority / interrupt_policy / created_at`。
- キュー操作：`enqueue | cancel(id) | clear | preempt`。
- **割り込みは3段階を区別**：
  1. **再生停止のみ**（現在の音を止める）
  2. **+ 進行中TTSリクエストもキャンセル**（httpx cancel）
  3. **+ キューも破棄**（clear）
- `interrupt_policy` 例：`none / stop_playback / cancel_current / flush_all`。API `POST /control` と GUI から発火。

## 7. 設定（toml）設計 ★codex#3,#4,#8 を反映して大幅拡張

READMEの中核要件「TTS への body を複数 toml で設定」を、**汎用HTTPドライバ＋スキーマ化したプロファイル**で実現。単純置換でなく**型を壊さない変数注入**（Jinja2相当 or 構造化テンプレ）。

### 7.1 TTSプロファイル schema（要素）
- `request.body_type = json | form | multipart | raw_template`
- `request.headers`（認証ヘッダ等）／`request.files`（multipart用）
- `template.vars`（`text` 以外に `speaker / emotion / lang` 等を将来注入）
- `response.mode = audio_stream | audio_file | json_base64 | json_url`
- `response.extract.path`（ネストJSONのフィールドパス）／`response.base64_format`
- `audio = AudioFormat(sample_rate, channels, sample_width, encoding)` … **raw/PCMでは必須メタ**
- `streaming_mode` の値型は **`bool | int | string` を許容**（GPT-SoVITS のバージョン差吸収）

### 7.2 例：`profiles/tts/gpt-sovits.toml`
```toml
[meta]
name   = "GPT-SoVITS (ローカル, api_v2)"
driver = "generic_http"

[request]
method      = "POST"
url         = "http://127.0.0.1:9880/tts"
body_type   = "json"
timeout_sec = 30
[request.headers]
"Content-Type" = "application/json"

[request.body]              # 値テンプレは型を保持（数値は数値のまま）
text           = "{{ text }}"
text_lang      = "ja"
ref_audio_path = "/mnt/holyland/Project/Text-to-shaberi/assets/ref.wav"
prompt_text    = "ある条件を満たすと、追ってくるモンスターを逆襲して食べることができる。"
prompt_lang    = "ja"
streaming_mode = 3          # 実測(2026-06-03):3でfirst-chunk270ms最速。int0-3可(2は439ms/中品質)
media_type     = "raw"
top_k          = 15
speed_factor   = 1.0

[response]
mode         = "audio_stream"   # audio_stream|audio_file|json_base64|json_url
content_type = "audio/raw"
# json系の例: extract.path="data.audio", base64_format="wav"

[audio]                          # raw/PCM のとき必須
encoding     = "pcm_s16le"
sample_rate  = 32000             # 確認済(2026-06-03): api_v2既定出力=pcm_s16le/32000/mono
channels     = 1
```

### 7.3 アプリ設定 `config.toml`
```toml
[app]
active_tts_profile = "gpt-sovits"

[stt]
backend  = "qwen_local"          # qwen_local | qwen_dashscope
language = "ja"                  # ⇔ Qwen "Japanese"（マッピング固定）
[stt.qwen_local]
model  = "Qwen/Qwen3-ASR-1.7B"
device = "cuda:0"
dtype  = "bfloat16"

[mic]
device      = "default"
sample_rate = 16000
channels    = 1
[mic.preprocess]
gate    = true                   # 軽量ノイズゲート（MVP既定ON）
denoise = "off"                  # off | rnnoise | noisereduce（任意・遅延計測対象）
[mic.vad]
engine         = "silero"        # silero | webrtc
min_silence_ms = 500
speech_pad_ms  = 150

[output]
device   = "default"             # "default" | デバイス名/ID（仮想オーディオ）
resample = "auto"                # 出力デバイスrateへ soxr 変換

[api]
enabled    = true
host       = "127.0.0.1"
port       = 8765
auth_token = ""                  # 空=ローカルのみ。外部公開時は必須
```

## 8. 音声I/O ★codex#8 を反映
- **AudioFormat を一次情報として扱う**：`(sample_rate, channels, sample_width, encoding)`。raw/PCMはヘッダ無しのため、プロファイルの `[audio]` を真実とする。
- **リサンプリング必須**：TTS出力rate（16k/24k/32k/44.1k等） → 出力デバイスrate（多くは48k）へ `soxr` で変換。誤ると速度/音程が壊れる。
- **出力デバイス切替**：再生中の安全な `drain → stop → reopen`。切替中のキュー保持/再開を定義。
- **wav応答**：ヘッダからフォーマット自動判定（rawはプロファイル指定優先）。
- **実装メモ(M1, 2026-06-03)**：既定出力デバイスは32kHzを拒否（PaErrorCode -9997）→ `player.play` で**デバイス既定rateへ soxr 変換**してから再生（M1へ前倒し・検証済）。**仮想sink `ttsvirt` への明示ルーティング**は PortAudio(ALSA) では個別sink名が列挙されず（`pipewire`/`default` 経由で既定sinkへ）、PipeWireレベルの結線（`pw-link` / `PULSE_SINK` / `node.target`）が必要 → **M2で対応**。

## 9. API 入力仕様（LLM 連携）★codex#9 を反映
- エンドポイント：
  - `POST /speak` … `{"text","profile?","priority?","interrupt_policy?"}` → Utterance enqueue
  - `WS /stream` … LLMトークンストリーム受信、文確定ごとに即TTS投入
  - `POST /control` … `{"command":"stop|cancel|clear"}`
- **ライフサイクル**：uvicorn を**別スレッド**で起動、アプリ終了時 **graceful shutdown**（M0/M3で実装）。ポート競合検知。
- **セキュリティ**：既定 `127.0.0.1`。`auth_token` 空ならローカルのみ、**外部公開時は token 必須**。WS切断/同時接続の扱いを定義。

## 10. リスク・未決事項

1. **総往復遅延（ボイスチェンジャー）**〔高〕：mic→VAD→STT→TTS→再生 が SLO に収まるか。**Spike で実測ゲート**。足りなければ DashScope realtime/別ASR（日本語要検証）。
2. **GPU同時常駐**〔高〕：3090Ti(24GB) 前提だが**検証ゲート**。起動時VRAM計測・ロード順固定・OOMフォールバック（`Spike-C`）。
3. **GPT-SoVITS streaming/raw**〔高〕：`streaming_mode` の型差、raw の sample_rate、first-chunk到達時間（`Spike-A`）。
4. **tomlドライバ表現力**〔高〕：JSON/form/multipart/base64/URL/stream/認証/ネストpath を schema で吸収できるか（§7）。
5. **ノイズ抑制とリアルタイム**〔中〕：MVPは軽量ゲートに限定、RNNoise等は任意・追加遅延を計測。
6. **割り込みUX**〔高〕：3段階割り込み（§6.2）の確定。
7. **Linux 仮想オーディオ**〔中〕：PipeWire/PulseAudio で仮想シンク作成（OBS/Discord連携）手順の確認。
8. **DashScopeリアルタイムの日本語**〔中〕：採用時に精度検証。

## 11. 実装フェーズ（性能PoCを前倒し）★codex#11 を反映

> **原則：UI実装より先に性能PoC(Spike)で最大リスクを潰す。**

- **Spike-A**：GPT-SoVITS `/tts` raw streaming の疎通＋**TTFA/first-chunk/sample_rate 実測**
- **Spike-B**：Qwen3-ASR-1.7B 単発＋疑似streaming の**STT遅延実測**（日本語）
- **Spike-C**：GPT-SoVITS と Qwen を**同時常駐したVRAM実測**＋OOM挙動
- **M0 足場**：uv/PySide6空窓、config・プロファイルローダ、デバイス列挙、FastAPI雛形＋graceful shutdown
- **M1 TTSコア**：toml駆動 generic_http → GPT-SoVITS同期再生（手入力→音声）＋疎通テスト
- **M2 ストリーミング再生＋音声I/O**：first-chunk再生、AudioFormat/soxrリサンプル、出力デバイス切替(drain/stop/reopen)
- **M3 キュー/割り込み/API**：Utterance＋優先度キュー、3段階割り込み、`/speak` `/stream` `/control`、token認証
- **M4 マイク+VAD**：常時録音・リングバッファ・軽量前処理・Silero VAD → 発話セグメント化
- **M5 STT統合**：Qwen3-ASRローカル → マイク→テキスト→TTS 往復完成、言語マッピング固定
- **M6 仕上げ**：レイテンシ計測UI、設定UI、ウォームアップ、OOM/エラー処理、DashScopeバックエンド、(任意)RNNoise

各段階で「テキスト→音声が鳴る」等の縦切り動作確認を優先。

## 12. 受け入れ基準（MVP）★codex#12 を反映：機能＋性能＋失敗時

**機能**
- [ ] テキスト手入力 → GPT-SoVITS で音声が鳴る（tomlでサーバ/ボディ設定）
- [ ] 出力先を既定/仮想オーディオで切替できる（切替中も破綻しない）
- [ ] `POST /speak` で外部（LLM想定）から発話、`/control` で割り込みできる
- [ ] マイク常時起動・無音検知で発話を区切り、Qwen-ASRでテキスト化 → 自動TTS

exec
/bin/bash -lc "sed -n '1,260p' docs/IMPL-M2.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# M2 実装計画書 v2: ストリーミング再生 ＋ 音声I/O

> 更新: v1 → **v2: codex(gpt-5.5) レビュー反映**（→ `docs/REVIEW-codex-M2.md`）。停止時のHTTPタスクcancel、`_build_request`の共有化、tail drain、24bit禁止、仮想sink候補選択規則、同時発話ポリシー等を明文化。
> フロー: 本書 → 実装は SecondOpinion(GLM-5.1) に委託 → 自分で検証/統合。
> 対象: `/mnt/holyland/Project/Text-to-shaberi`（Python3.12 / uv / PySide6 / qasync）。**この仕様の通りに実装すること（曖昧な箇所は本書の規定に従う）。**

## 1. 目的・スコープ
M1 の「全受信→一括再生」を **チャンク逐次再生** に置き換え、TTFA を短縮（§3 SLO, 狙い ~270ms）。出力デバイス切替の実用化、停止ボタン、仮想sink `TTS_Virtual`(ttsvirt) への配信ルーティング(best-effort)。
非スコープ: キュー（M3）、マイク/STT（M4/M5）。ただし停止/abort と HTTPタスクcancel は M3 の素地として正しく作る。

## 2. 現状(M1)の関連実装（変更の起点）
- `tts/client.py`: `TTSClient.synthesize(profile, text) -> AudioBuffer`、`_build_request(profile, body)->httpx.Request`、`_decode_raw/_decode_wav`、`_PCM_DTYPE`、`AudioBuffer`。
- `audio/player.py`: `play(samples, sample_rate, device)`、`_device_samplerate(sd, device)`、`stop()`。
- `audio/devices.py`: `preferred_default_output()`、`list_output_devices()`、`resolve_device()`。
- `ui/main_window.py`: 出力先 combo（"既定…"=None / 各デバイス index）、`set_on_speak`、`selected_output_device()->int|None`、`log()`。
- `app.py: run_gui`: `speak(text)` が `synthesize`→`run_in_executor(play)`、`app.aboutToQuit`→`client.aclose()`。
- プロファイル実値: `response.mode=audio_stream`, `audio.encoding=pcm_s16le`, `sample_rate=32000`, `channels=1`, `streaming_mode=3`, `media_type=raw`。

## 3. 設計（確定仕様）

### 3.1 リクエスト構築の共通化（`tts/client.py`）
- **`_build_request_kwargs(self, profile, body) -> tuple[str, str, dict]`** を新設し、`(method, url, kwargs)` を返す。`kwargs` は `headers`, `timeout`, および body種別に応じて **いずれか1つ**：
  - `profile.method == "GET"` → `params=body`
  - `body_type == "json"` → `json=body`
  - `body_type in ("form","multipart")` → `data=body`（**multipartのファイル送信はM2では未対応**、コメントで明記）
  - その他 → `json=body`
- `synthesize()` は `method,url,kwargs=_build_request_kwargs(...)` → `resp = await self._client.request(method, url, **kwargs)`。
- 既存 `_build_request`(httpx.Request返し) は削除し `_build_request_kwargs` に一本化（呼び出し側を更新）。

### 3.2 ストリーミング受信（`tts/client.py`）
- **`async def stream(self, profile, text, **variables) -> AsyncIterator[bytes]`**
  - 適用条件: `profile.response_mode == "audio_stream"`（呼び出し側が判定。それ以外は `synthesize` を使う）。
  - 実装:
    ```python
    method, url, kwargs = self._build_request_kwargs(profile, profile.render_body(text, **variables))
    async with self._client.stream(method, url, **kwargs) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
    ```
  - **キャンセル安全**: 呼び出し側が `Task.cancel()` した場合、`async with` が確実にHTTP接続を閉じること（受け入れ基準）。

### 3.3 PCMデコード共通化（`tts/client.py`）
- **`_pcm_to_float32(data: bytes, encoding: str, channels: int) -> np.ndarray`** を新設（`_decode_raw` と `StreamingPlayer` で共用）。
  - 対応 encoding と変換:
    - `pcm_s16le`: dtype `<i2`、`/32768.0`
    - `pcm_s32le`: dtype `<i4`、`/2147483648.0`
    - `pcm_f32le`: dtype `<f4`、そのまま
    - **上記以外（`pcm_s24le` 含む）は `raise ValueError(f"unsupported encoding: {encoding}")`。黙って `<i2` にフォールバックしない。**
  - 戻り shape: **mono(channels==1) は 1D `(n,)`、多chは 2D `(-1, channels)`**。`data` は呼び出し側が「完全サンプル境界」に整えて渡す前提（端数は渡さない）。
- **`bytes_per_sample(encoding) -> int`**: s16le=2 / s32le=4 / f32le=4 / それ以外は `ValueError`。
- サンプル境界分割を**純関数**に切り出してテスト可能にする: **`split_full_samples(buf: bytes, frame_bytes: int) -> tuple[bytes, bytes]`** が `(完全フレーム分, 端数carry)` を返す（`frame_bytes = bytes_per_sample*channels`）。
- 既存 `_PCM_DTYPE`/`_decode_raw` は `_pcm_to_float32` を使う形に整理（24bit近似は削除）。`AudioBuffer.samples` のコメントを「mono=1D / 多ch=2D」に修正。

### 3.4 ストリーミング再生（`audio/player.py`）
- **`class StreamingPlayer`**
  - `__init__(self, src_rate:int, channels:int=1, device:int|None=None, encoding:str="pcm_s16le", preroll_ms:int=40)`:
    - `bytes_per_sample(encoding)` を検証（不正なら `ValueError`）。
    - `device is None` → `preferred_default_output()`。
    - `self._target_sr = _device_samplerate(sd, device)`。
    - rate差があれば `self._resampler = soxr.ResampleStream(src_rate, self._target_sr, channels, dtype="float32", quality="HQ")`、無ければ None。
    - `self._stream = sd.OutputStream(samplerate=self._target_sr, channels=channels, dtype="float32", device=device, latency="low")`; `self._stream.start()`。
    - `self._carry = b""`、`self._frame_bytes = bytes_per_sample(encoding)*channels`、`self._preroll = []`（list[np.ndarray]）、`self._preroll_frames = int(self._target_sr*preroll_ms/1000)`、`self._started_audio = False`。
  - **`write_bytes(self, data: bytes) -> None`**:
    1. `full, self._carry = split_full_samples(self._carry + data, self._frame_bytes)`。
    2. `if not full: return`。
    3. `frames = _pcm_to_float32(full, self._encoding, self._channels)`。
    4. `if self._resampler is not None: frames = self._resampler.resample_chunk(frames)`。
    5. `if len(frames)==0: return`。
    6. **プリロール吸収**: `started_audio` が False の間は `self._preroll.append(frames)`。合計フレーム数が `self._preroll_frames` 以上になったら連結して1回 `self._stream.write(...)` し `started_audio=True`。以降は各 `frames` を即 `self._stream.write(frames)`。
    7. `OutputStream.write` 例外時は `abort()` して再 raise。
  - **`close(self) -> None`**（正常終了。末尾を切らない）:
    - resampler があれば `tail = self._resampler.resample_chunk(np.empty((0,) if channels==1 else (0,channels), dtype=np.float32), last=True)`。
    - プリロール未flushなら残りを連結。`tail`＋未flush分を write。
    - **tail drain**: `time.sleep(float(self._stream.latency) + 0.05)` で PortAudio バッファを鳴らし切ってから `self._stream.stop(); self._stream.close()`（`stop()` だけに drain を依存しない）。
  - **`abort(self) -> None`**（即停止）: `self._stream.abort(); self._stream.close()`（idempotent に。二重closeはガード）。
- `play()`(M1) は wav/json・非stream用に**据え置き**。

### 3.5 app.py 統合（`run_gui`）
- **起動時（最優先・QApplication生成前かつ sounddevice import/`_load_output_devices()` 前）**: pulse client 名を固定するため
  `os.environ.setdefault("PULSE_PROP", f"application.name=text-to-shaberi media.role=production")` を設定。
- 状態保持: `current_task: list[asyncio.Task|None] = [None]`、`current_player: list[StreamingPlayer|None] = [None]`（クロージャ用に1要素list）。
- `speak(text)`（async）:
  ```python
  device = window.selected_output_device()           # int | None | "__ttsvirt__"
  use_virtual = device == "__ttsvirt__"
  play_device = None if use_virtual else device       # 仮想は pipewire(None→preferred) に流す
  window.set_busy(True)                                # 送信ボタン disable（同時発話防止）
  player = None
  try:
      current_task[0] = asyncio.current_task()
      if profile.response_mode == "audio_stream":
          player = await loop.run_in_executor(None, lambda: StreamingPlayer(
              profile.audio.sample_rate, profile.audio.channels, play_device, profile.audio.encoding))
          current_player[0] = player
          if use_virtual:
              moved = await loop.run_in_executor(None, lambda: move_app_to_sink("ttsvirt", "text-to-shaberi", os.getpid()))
              window.log(f"[route] TTS_Virtual move: {moved}")
          t0 = loop.time(); first_chunk = None
          async for chunk in client.stream(profile, text):
              if first_chunk is None:
                  first_chunk = loop.time()-t0
                  window.log(f"[tts] first-chunk {first_chunk*1000:.0f}ms")
              await loop.run_in_executor(None, player.write_bytes, chunk)
          await loop.run_in_executor(None, player.close)
          window.log(f"[tts] 再生完了 {loop.time()-t0:.2f}s")
      else:
          buf = await client.synthesize(profile, text)
          await loop.run_in_executor(None, play_audio, buf.samples, buf.sample_rate, play_device)
  except asyncio.CancelledError:
      if player is not None:
          await loop.run_in_executor(None, player.abort)
      window.log("[tts] 停止しました")
      # 正常停止扱い。再raiseしない。
  except Exception as exc:
      if player is not None:
          await loop.run_in_executor(None, player.abort)
      window.log(f"[error] {exc!r}")
  finally:
      current_task[0] = None
      current_player[0] = None
      window.set_busy(False)
  ```
- `on_speak = lambda text: (current_task[0] is None) and asyncio.ensure_future(speak(text))`（busy中は send disable なので二重起動しない設計だが、ガードも入れる）。
- **停止** `on_stop()`: `if current_player[0]: loop.run_in_executor(None, current_player[0].abort)`; `if current_task[0]: current_task[0].cancel()`。
- **aboutToQuit**: `if current_task[0]: current_task[0].cancel()`; `if current_player[0]: player.abort()`; その後 `asyncio.ensure_future(client.aclose())`。

### 3.6 出力デバイス切替・同時発話ポリシー
- 各発話で `selected_output_device()` を都度参照（切替は次発話から反映）。combo変更時に再生中なら「次発話から切替わります」をログ（任意）。
- **同時発話**: M2は「再生中は送信ボタンを無効化」。`MainWindow.set_busy(bool)` を追加し、送信中は `text_input`/`send_button` を disable、`stop_button` のみ有効。完了/停止/エラーで解除。

### 3.7 仮想sink ルーティング（`audio/routing.py` 必須・新規）
- **`move_app_to_sink(sink_name: str, app_name: str, pid: int) -> int | None`**（移動した sink-input id、失敗時 None）:
  1. `shutil.which("pactl")` 無→ログ「pactl不在」→ None。
  2. `pactl list short sinks` に `sink_name`(ttsvirt) 無→ログ「sink不在」→ None。
  3. `pactl list sink-inputs` を `subprocess.run([...], capture_output=True, text=True, timeout=3)`（**shell不使用**）で取得し、各 "Sink Input #N" ブロックの `application.process.id` と `application.name` を抽出。
  4. 候補 = `application.process.id == str(pid)` **かつ** `application.name == app_name`。
     - 0件 → ログ「sink-input未検出」→ None。
     - 複数 → **最大の#id（最も新しい）** を採用しログに候補数を出す。
  5. `pactl move-sink-input <id> <sink_name>`（timeout付）。成功→id返す。失敗→ログ→None。
- すべて例外を捕捉し**クラッシュさせない**。各失敗を個別ログ。
- 注意: sink-input は **OutputStream.start() 後** に出現するため、`move_app_to_sink` は再生開始直後（最初の write 前後）に呼ぶ。PULSE_PROP は §3.5 の通り起動時設定。

### 3.8 UI（`ui/main_window.py`）
- 「停止」ボタン追加、`set_on_stop(callback)`。
- 出力先 combo に項目 **「TTS_Virtual（配信用）」**（`addItem(..., "__ttsvirt__")`）追加。
- `selected_output_device() -> int | None | str`（戻り型を更新）。
- `set_busy(busy: bool)`: 送信中UIの有効/無効。

## 4. 受け入れ基準
- [ ] 既定デバイスで streaming 再生。`first-chunk` をログ表示（≤400ms, 狙い~270ms）。
- [ ] underrun/境界ノイズ無く**末尾まで**再生（短いsineで末尾欠け無しを手動確認）。
- [ ] 出力先デバイス変更が次発話で反映。
- [ ] 「停止」で ~150ms 程度で無音化し、**HTTPストリームも閉じる**（task cancel）。閉じた stream への write 例外でクラッシュしない。
- [ ] 再生中は送信ボタンが無効（同時発話で音が重ならない）。
- [ ] 「TTS_Virtual」選択で OBS等の「Monitor of TTS_Virtual」に流れる（best-effort、失敗時は既定sink＋警告ログ・**非クラッシュ**）。
- [ ] アプリ終了時に進行中 task/player を止めてから終了。
- [ ] `py_compile` 全モジュール成功、後述ユニットテスト成功。

## 5. 変更/新規ファイル
- `tts/client.py`（`_build_request_kwargs`/`stream`/`_pcm_to_float32`/`bytes_per_sample`/`split_full_samples`、`_decode_raw`整理）
- `audio/player.py`（`StreamingPlayer` 追加）
- `audio/routing.py`（**新規・必須**：`move_app_to_sink`）
- `ui/main_window.py`（停止ボタン/`set_on_stop`/`set_busy`/TTS_Virtual項目/`selected_output_device`型更新）
- `app.py`（streaming統合、停止結線、PULSE_PROP、仮想sink移動、aboutToQuit強化）
- `tests/test_pcm.py`（**新規**：`_pcm_to_float32` と `split_full_samples` のユニットテスト）

## 6. テスト
- **ユニット（sounddevice不要・必須）**: `tests/test_pcm.py`
  - `split_full_samples(b"\x01\x02\x03", 2) == (b"\x01\x02", b"\x03")`、奇数byte分割でcarryが繰越されること。
  - `_pcm_to_float32` が s16le/s32le/f32le を正しく正規化し、mono=1D/2ch=2D を返すこと。未知encodingで `ValueError`。
- **ヘッドレス（GPT-SoVITS :9880 起動前提）**: `client.stream` で first-chunk 時刻を計測（~270ms）。`py_compile src/**/*.py`。
- **GUI（ユーザー確認）**: 実音・first-chunk表示・末尾欠け無し・デバイス切替・停止(無音化&task cancel)・送信disable・TTS_Virtual→OBS。

## 7. 留意点
- `run_in_executor` の write は順序保証のため**逐次 await**。
- soxr mono入力は 1D、多chは (-1,channels)。
- `OutputStream(latency="low")`。プリロール ~40ms でジッタ吸収。tail は sleep(latency+0.05) で drain。
- routing は環境依存で best-effort。確実版（`pw-link`/`node.target`）は **M2b** に分離（本M2では非ブロッキングで失敗許容）。
- subprocess は timeout・`capture_output=True`・`text=True`・shell不使用。

codex
ARCH v0.3 は全体方針としては自然ですが、既存M2で固めた「逐次writeによる順序保証」「lazy-start/プリロール」「HTTP cancel」などの実装上の前提が、WS化で少し薄くなっています。次にコード側を見て、移行マップが実際に成立するか確認します。
exec
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/tts/client.py" in /mnt/holyland/Project/Text-to-shaberi
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
/bin/bash -lc "sed -n '1,260p' docs/CODE-REVIEW-M2.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# コードレビュー: M2 実装（GLM-5.1 委託分）

レビュアー: Claude（統合担当）。対象: GLMが `docs/IMPL-M2.md v2` に基づき実装した6+1ファイル。

## 総合判定: 合格（1件の要修正バグあり・C1で対応）
仕様 `IMPL-M2.md v2` に忠実。検証も通過：
- `py_compile` 全7モジュール OK
- ユニットテスト `tests/test_pcm.py` **15/15 passed**
- 実サーバ(:9880) `client.stream` **first-chunk = 148ms**（目標≤400ms）
- `synthesize` 回帰 OK（M1経路維持）
- `StreamingPlayer` 無音スモーク（奇数長chunkでcarry誘発）例外なし

## 良い点
- `_build_request_kwargs` 一本化で `synthesize`/`stream` が同一ロジック（codex指摘#1反映）。
- `stream` は `async with self._client.stream(...)` で **cancel安全**（codex#2反映）。
- `_pcm_to_float32` は未知encodingで `ValueError`（暗黙fallback無し）、24bitは非対応で安全（codex#4反映）。
- `StreamingPlayer`: carry境界処理、preroll、tail-drain（sleep(latency)後close）、idempotentなclose/abort、write例外でabort+raise（codex#3,#7反映）。
- `routing.move_app_to_sink`: pactl存在/sink存在/候補(pid+app名)選択/最新id/timeout付subprocess/全失敗ログ＆None（codex#8,#9反映）。
- UI: 停止/`set_busy`(同時発話防止)/TTS_Virtual項目/戻り型 `int|None|str`（codex#10反映）。

## 要修正（1件）
### [高] ストリーミング再生で発話の先頭が欠ける
- 症状（ユーザー実機）: 最初が途切れて読まれない。
- 原因: `StreamingPlayer.__init__` で `OutputStream.start()` を即実行するため、最初のチャンク到達(~148ms)まで**枯渇状態で空回り**し、頭が飲まれる。preroll(40ms)は `start()` 後の蓄積で、空回り自体は防げていない。
- 修正方針（**C1で実装**・再生はクライアント側に残るため有効）:
  - **遅延スタート**: `__init__` では `OutputStream` を生成のみ（`start()` しない）。最初に preroll が満たされて書き込む直前に `start()`。
  - preroll を ~80–150ms に増やしジッタ吸収（TTFAは +preroll するが LAN想定で目標内）。
  - `close`/`abort` は未スタートでも安全に（既存の `_closed` ガードに加え未start時の stop/close をガード）。

## 軽微
- `app.py` の `PULSE_PROP` 設定行が**プレースホルダ無しの f-string**（ruff F541相当）。`f"..."` を通常文字列に。
- `routing.py` の `move-sink-input` は `returncode` 未チェック（失敗でも成功ログ）。best-effortだが `result.returncode` を見てログを分けると堅牢。
- `tests/` のため pytest を dev依存グループ(PEP735)に追加済（妥当）。

## 注記（v0.3 への影響）
- 本M2コードは **クライアント–サーバ分離(v0.3)** で配置換えされる：`StreamingPlayer`/`devices`/`ui`→クライアント、`tts client/profile`→サーバ。`routing.py`(PipeWire)はLinux/dev用、Windows本番はデバイス選択。
- 本コミットは **分離リファクタ前のM2チェックポイント**として保存する。

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
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/audio/routing.py" in /mnt/holyland/Project/Text-to-shaberi
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

codex
コード上はM2の主要資産は再利用できます。ただし `audio/player.py` が `tts.client` のPCMヘルパーへ依存していて、単純移設するとクライアント側がサーバ側TTSモジュールへ引きずられるので、§8の「配置換え」は依存方向の修正込みで書く必要があります。
exec
/bin/bash -lc "sed -n '1,320p' src/text_to_shaberi/config.py" in /mnt/holyland/Project/Text-to-shaberi
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
/bin/bash -lc "sed -n '1,320p' src/text_to_shaberi/tts/profile.py" in /mnt/holyland/Project/Text-to-shaberi
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

codex
設定とプロファイルの現状も確認しました。現行 `pyproject.toml` はまだ全依存一括で、`torch/qwen/silero-vad` がトップレベル依存なので、R0は単なるディレクトリ分割ではなく依存境界の実証が必要です。
exec
/bin/bash -lc "nl -ba src/text_to_shaberi/audio/player.py | sed -n '1,220p'" in /mnt/holyland/Project/Text-to-shaberi
exec
/bin/bash -lc "nl -ba docs/ARCH-v0.3-client-server.md | sed -n '1,220p'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
     1	"""音声再生（M2: ブロッキング再生＋ストリーミング逐次再生）。
     2	
     3	出力デバイスは TTS の出力rate（例 32000Hz）を受け付けないことがある（生のALSA hwデバイス等は
     4	44100/48000固定）。そのため**出力デバイスの既定rateへ soxr で変換**してから再生する。
     5	M2 で OutputStream を使ったチャンク逐次再生（StreamingPlayer）を追加。
     6	"""
     7	from __future__ import annotations
     8	
     9	import logging
    10	import time
    11	
    12	import numpy as np
    13	
    14	from ..tts.client import _pcm_to_float32, bytes_per_sample, split_full_samples
    15	
    16	logger = logging.getLogger(__name__)
    17	
    18	
    19	def _device_samplerate(sd, device: int | None) -> int:
    20	    info = sd.query_devices(kind="output") if device is None else sd.query_devices(device)
    21	    return int(info["default_samplerate"])
    22	
    23	
    24	def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    25	    """samples(float32 [-1,1]) を device で再生し、完了まで待つ（ブロッキング）。
    26	
    27	    出力デバイスの既定rateへ必要に応じてリサンプルする。別スレッド(executor)から呼ぶ前提。
    28	    """
    29	    import sounddevice as sd
    30	
    31	    if device is None:
    32	        from .devices import preferred_default_output
    33	
    34	        device = preferred_default_output()
    35	
    36	    target_sr = _device_samplerate(sd, device)
    37	    data = samples
    38	    if target_sr != sample_rate:
    39	        import soxr
    40	
    41	        data = soxr.resample(samples, sample_rate, target_sr)
    42	    sd.play(data, samplerate=target_sr, device=device)
    43	    sd.wait()
    44	
    45	
    46	def stop() -> None:
    47	    import sounddevice as sd
    48	
    49	    sd.stop()
    50	
    51	
    52	class StreamingPlayer:
    53	    """OutputStream + soxr リサンプルでチャンク逐次再生。
    54	
    55	    使い方:
    56	        player = StreamingPlayer(src_rate, channels, device, encoding)
    57	        for chunk in http_stream:
    58	            player.write_bytes(chunk)
    59	        player.close()   # 正常終了（末尾まで鳴らす）
    60	        # または player.abort()  # 即停止
    61	    """
    62	
    63	    def __init__(
    64	        self,
    65	        src_rate: int,
    66	        channels: int = 1,
    67	        device: int | None = None,
    68	        encoding: str = "pcm_s16le",
    69	        preroll_ms: int = 40,
    70	    ) -> None:
    71	        import sounddevice as sd
    72	        import soxr
    73	
    74	        self._encoding = encoding
    75	        self._channels = channels
    76	        self._frame_bytes = bytes_per_sample(encoding) * channels
    77	
    78	        if device is None:
    79	            from .devices import preferred_default_output
    80	
    81	            device = preferred_default_output()
    82	
    83	        self._target_sr = _device_samplerate(sd, device)
    84	
    85	        self._resampler: soxr.ResampleStream | None = None
    86	        if self._target_sr != src_rate:
    87	            self._resampler = soxr.ResampleStream(
    88	                src_rate, self._target_sr, channels, dtype="float32", quality="HQ"
    89	            )
    90	
    91	        self._stream = sd.OutputStream(
    92	            samplerate=self._target_sr,
    93	            channels=channels,
    94	            dtype="float32",
    95	            device=device,
    96	            latency="low",
    97	        )
    98	        self._stream.start()
    99	        self._closed = False
   100	
   101	        self._carry = b""
   102	        self._preroll: list[np.ndarray] = []
   103	        self._preroll_frames = int(self._target_sr * preroll_ms / 1000)
   104	        self._started_audio = False
   105	
   106	    def write_bytes(self, data: bytes) -> None:
   107	        """PCM チャンクを受け取り、サンプル境界に整えてから OutputStream へ書き込む。"""
   108	        full, self._carry = split_full_samples(self._carry + data, self._frame_bytes)
   109	        if not full:
   110	            return
   111	
   112	        frames = _pcm_to_float32(full, self._encoding, self._channels)
   113	
   114	        if self._resampler is not None:
   115	            frames = self._resampler.resample_chunk(frames)
   116	        if len(frames) == 0:
   117	            return
   118	
   119	        if not self._started_audio:
   120	            self._preroll.append(frames)
   121	            total = sum(len(f) for f in self._preroll)
   122	            if total >= self._preroll_frames:
   123	                combined = np.concatenate(self._preroll)
   124	                self._preroll.clear()
   125	                try:
   126	                    self._stream.write(combined)
   127	                except Exception:
   128	                    self.abort()
   129	                    raise
   130	                self._started_audio = True
   131	        else:
   132	            try:
   133	                self._stream.write(frames)
   134	            except Exception:
   135	                self.abort()
   136	                raise
   137	
   138	    def close(self) -> None:
   139	        """正常終了。resampler 残差を flush し、tail drain 後に stop+close。"""
   140	        if self._closed:
   141	            return
   142	        self._closed = True
   143	
   144	        tail: np.ndarray | None = None
   145	        if self._resampler is not None:
   146	            empty = (
   147	                np.empty((0,), dtype=np.float32)
   148	                if self._channels == 1
   149	                else np.empty((0, self._channels), dtype=np.float32)
   150	            )
   151	            tail = self._resampler.resample_chunk(empty, last=True)
   152	
   153	        chunks: list[np.ndarray] = []
   154	        if tail is not None and len(tail) > 0:
   155	            chunks.append(tail)
   156	        if self._preroll:
   157	            chunks.extend(self._preroll)
   158	            self._preroll.clear()
   159	
   160	        if chunks:
   161	            try:
   162	                self._stream.write(np.concatenate(chunks))
   163	            except Exception:
   164	                pass
   165	
   166	        time.sleep(float(self._stream.latency) + 0.05)
   167	        try:
   168	            self._stream.stop()
   169	            self._stream.close()
   170	        except Exception:
   171	            pass
   172	
   173	    def abort(self) -> None:
   174	        """即停止（idempotent）。"""
   175	        if self._closed:
   176	            return
   177	        self._closed = True
   178	        try:
   179	            self._stream.abort()
   180	        except Exception:
   181	            pass
   182	        try:
   183	            self._stream.close()
   184	        except Exception:
   185	            pass

 succeeded in 0ms:
     1	# アーキテクチャ改訂 v0.3: クライアント–サーバ分離
     2	
     3	> 本書は `docs/PLAN.md` v0.2（モノリシック前提）を**改訂**する。ヒアリング(2026-06-03)で
     4	> 「フロント=Windows(マイク)／処理=ヘッドレスGPUサーバ」という分離構成が確定したため。
     5	> フロー: 本書を codex レビュー → 反映 → 各マイルストーンの実装計画 → GLM 委託 → 検証。
     6	
     7	## 1. 変更点（モノリシック → 分離）
     8	- 旧: 1プロセスで mic→STT→TTS→再生（GPUマシン上）。
     9	- 新: **Windowsクライアント（GUI/マイク/VAD/再生）** ↔ **WebSocket** ↔ **ヘッドレスGPUサーバ（STT/TTS）**。
    10	- 既存実装(M0足場/M1 TTSコア/M2 StreamingPlayer/IME)は**捨てず配置換えして再利用**（§8 移行マップ）。
    11	
    12	## 2. 全体アーキテクチャ
    13	```
    14	 Windows クライアント                              Linux GPU サーバ (headless)
    15	┌───────────────────────────┐   WebSocket   ┌──────────────────────────────┐
    16	│ PySide6 GUI                │◄─ws(s)://────►│ WS backend (FastAPI/websockets)│
    17	│ マイク取得 + VAD(Silero)    │  制御=JSON     │  ├ Orchestrator(セッション/割込) │
    18	│ テキスト入力 / API入力(LLM) │  音声=binary   │  ├ STT: Qwen3-ASR (GPU常駐)      │
    19	│ StreamingPlayer 再生        │  (PCM/Opus自動)│  ├ TTS: GPT-SoVITSクライアント   │
    20	│ 出力デバイス選択(VB-CABLE等) │                │  │      (tomlプロファイル)        │
    21	└───────────────────────────┘                │  └ Codec(PCM/Opus) + 認証/TLS   │
    22	                                              └───────────────┬──────────────┘
    23	                                                GPT-SoVITS :9880 (別プロセス・既存)
    24	```
    25	
    26	## 3. リポジトリ / 依存の再編
    27	```
    28	src/text_to_shaberi/
    29	├── common/    # 共有: protocol(WSメッセージ&スキーマ) / audio_format / codec(PCM,Opus) / 設定共通
    30	├── server/    # バックエンド: ws_server / orchestrator / stt(Qwen) / tts(GPT-SoVITSクライアント+profile)
    31	├── client/    # フロント: ui(PySide6) / mic(sounddevice+VAD) / player(StreamingPlayer) / devices / ws_client
    32	└── (entry)    # text-to-shaberi-server  /  text-to-shaberi-client
    33	```
    34	- **依存グループ**（`pyproject` optional-dependencies）:
    35	  - `server`: torch(cu128), qwen-asr, fastapi, uvicorn, websockets, httpx, numpy, (soxr)
    36	  - `client`: PySide6, qasync, sounddevice, soxr, numpy, websockets, (opus: `opuslib`/`PyOgg`)
    37	  - `common`: numpy, pydantic, tomllib(標準)
    38	- **Windowsクライアントは client+common のみインストール**（torch/qwen 不要）。サーバは server+common。
    39	- 設定: `server.toml` / `client.toml` に分割（共通ローダ）。TTSプロファイルはサーバ側 `profiles/tts/*.toml`。
    40	- **クライアントUI方針**: メイン画面は**シンプル維持**（入力・送信・停止・出力先・ログ）。増えていく設定は**歯車(設定)ボタン → 設定ダイアログ**に集約する（ハンバーガーでも可）。設定ダイアログの項目: **サーバWSアドレス / 認証トークン / 接続(LAN・遠隔) / コーデック(自動/PCM/Opus) / 出力デバイス / TTSプロファイル選択 / 将来項目**。設定は `client.toml` に永続化し、起動時ロード・変更時保存。機能トグル（例: 自動TTS, マイクON/OFF）も同ダイアログに。
    41	
    42	## 4. WebSocket プロトコル（v1）
    43	- 接続: `ws(s)://host:port/ws`。認証は接続時 `Authorization: Bearer <token>`（または最初の `hello` に同梱）。
    44	- 制御は**テキスト(JSON)フレーム**、音声は**バイナリフレーム**。M-scopeは方向ごとに**同時1ストリーム**前提（多重化はキュー導入時に拡張）。
    45	- ハンドシェイク:
    46	  - C→S `{"type":"hello","caps":{"codecs":["pcm_s16le","opus"]}}`
    47	  - S→C `{"type":"ready","codec":"pcm_s16le","tts":{"sample_rate":32000,"channels":1}}`（環境/設定でcodec決定）
    48	- 発話(テキスト→音声):
    49	  - C→S `{"type":"speak","id":"u1","text":"…","profile":"gpt-sovits","interrupt":false}`
    50	  - S→C `{"type":"audio_begin","id":"u1","format":{"codec":"pcm_s16le","sample_rate":32000,"channels":1}}`
    51	  - S→C `<binary audio chunk> …`（直近 audio_begin に属する）
    52	  - S→C `{"type":"audio_end","id":"u1"}`
    53	- 音声認識(マイク→テキスト):
    54	  - C→S `{"type":"stt_begin","id":"u2","format":{"codec":"pcm_s16le","sample_rate":16000,"channels":1}}`
    55	  - C→S `<binary audio chunk> …`（発話セグメント）
    56	  - C→S `{"type":"stt_end","id":"u2"}`
    57	  - S→C `{"type":"transcript","id":"u2","text":"…","final":true}`
    58	  - サーバ設定で **自動TTS** なら続けて audio_begin/…/audio_end も送る。
    59	- 制御/エラー:
    60	  - C→S `{"type":"stop","id":"u1"}` → サーバはTTS/STT生成をキャンセル、`{"type":"stopped","id":"u1"}`。
    61	  - S→C `{"type":"error","id":…,"message":"…"}`
    62	- スキーマは `common/protocol.py`（pydantic）で型定義し client/server で共有。
    63	
    64	## 5. 音声コーデック戦略（自動 PCM/Opus）
    65	- `common/codec.py`: `encode(frames,fmt)->bytes` / `decode(bytes,fmt)->frames`。`pcm_s16le`(無変換) と `opus`(48k/20msフレーム) を実装。
    66	- ネゴシエーション: サーバ設定 `transport="auto"|"pcm"|"opus"`。`auto` は接続元が loopback/LAN(プライベートIP) なら PCM、それ以外 Opus。`hello.caps` と突き合わせて `ready.codec` 決定。
    67	- TTS出力(32kHz)→Opus(48k)変換は soxr。受信側で逆変換。**rawは AudioFormat を audio_begin で明示**（§PLAN §8 の原則を踏襲）。
    68	
    69	## 6. セキュリティ
    70	- LAN: `token` 任意（既定は共有トークン1個）。遠隔: **token必須 ＋ wss/TLS必須**。
    71	- サーバは既定 `0.0.0.0` ではなく設定で bind を明示。CORS/Origin チェック。レート制限は後続。
    72	
    73	## 7. レイテンシ予算（ネットワーク込みで再評価）
    74	- LAN: WS往復 ~1–5ms。**GUI/API TTFA = 送信 + net + サーバ(GPT-SoVITS first-chunk 148–270ms 実測) + net + 復号/再生** ≈ ローカル+5–15ms → **目標 p50≤400ms 維持可**。
    75	- マイク往復 = mic+VAD(端末) + net + STT(0.29s) + TTS first-chunk + net + 再生 ≈ LANで+10–20ms。
    76	- Opus enc/dec ~数ms。遠隔は RTT 次第（受け入れ基準は LAN を主、遠隔は best-effort）。
    77	
    78	## 8. 既存コードの移行マップ（M0–M2 の配置換え）
    79	| 既存 | 移行先 | 備考 |
    80	|---|---|---|
    81	| `tts/client.py`(TTSClient/stream/decode) | `server/tts/` | サーバ内でGPT-SoVITSをstreaming |
    82	| `tts/profile.py` | `server/tts/`（一部fmtは`common`） | tomlプロファイル |
    83	| `audio/player.py`(StreamingPlayer) | `client/audio/` | **lazy-start修正込み**で再利用 |
    84	| `audio/devices.py` | `client/audio/` | Windowsは VB-CABLE をデバイス選択 |
    85	| `audio/routing.py`(PipeWire) | `client/audio/`(Linux/dev用) | Windows本番では未使用 |
    86	| `ui/main_window.py` | `client/ui/` | WS送信に結線 |
    87	| `api/server.py`(FastAPI) | `server/ws_server` | WSバックエンドへ昇格(REST /speak も残置可) |
    88	| `config.py` | `common`＋server/client個別 | server.toml/client.toml |
    89	| `app.py` | `client`(run_gui)＋`server`(run_server) | エントリ2つ |
    90	
    91	## 9. 改訂マイルストーン
    92	- **R0 再編**: common/server/client へ分割、依存グループ、`common/protocol.py`(pydantic)、既存コード移設、両エントリの空起動。
    93	- **S1 サーバTTS-WS**: `speak`→GPT-SoVITS streaming→PCM音声フレーム送信＋`stop`キャンセル。
    94	- **C1 クライアント再生**: GUI→WS `speak`→受信→**StreamingPlayer(lazy-start修正)** 再生。LAN/PCMで「テキスト→声」疎通＋TTFA計測。＋**歯車ボタンの設定ダイアログ最小版**（サーバWSアドレス/トークンを設定→`client.toml`保存、再接続）。
    95	- **S2 サーバSTT-WS**: 音声受信→Qwen→`transcript`（＋自動TTSオプション）。
    96	- **C2 マイク+VAD**: 端末マイク常時録音・Silero VAD・発話セグメント→WS→transcript/音声往復（マイク往復完成）。
    97	- **X1 コーデック/セキュリティ**: PCM/Opus自動＋token認証＋wss/TLS（遠隔対応）。
    98	- **X2 仕上げ**: ネット込みレイテンシ計測・WS再接続/バックプレッシャ・設定UI・Windows実機(VB-CABLE)・エラー処理。
    99	
   100	## 10. リスク・未決
   101	1. **Windows音声(WASAPI)** での sounddevice/StreamingPlayer 挙動（latency, lazy-start, デバイス列挙, VB-CABLE）→ 実機検証。
   102	2. **Opusの依存**（`opuslib`/`PyOgg` のWindowsホイール入手性）→ 無ければ PCM 限定で先行。
   103	3. **WSバイナリのid関連付け**（M-scopeは方向ごと単一ストリーム前提。多重化はキュー導入で）。
   104	4. **Silero VAD のWindows動作**（torch依存の有無。webrtcvadフォールバック検討）。
   105	5. **再接続/切断時**のセッション復帰・バックプレッシャ（送信過多で詰まらせない）。
   106	6. STT結果→自動TTSの**サーバ内連結**か、クライアント往復かのフロー確定（設定で選べる形を推奨）。
   107	
   108	## 11. 受け入れ基準（疎通MVP）
   109	- [ ] サーバ(headless)起動＋クライアント(GUI)が ws 接続・`hello/ready` 成立。
   110	- [ ] テキスト送信→サーバTTS→クライアント再生（**先頭欠け無し**）。**TTFA(ネット込み, LAN) ≤ 400ms** をログ表示。
   111	- [ ] マイク発話→サーバSTT→transcript表示→（自動TTSなら）再生。
   112	- [ ] `stop` で音停止＋サーバ生成キャンセル。
   113	- [ ] LAN/PCM と Opus 双方で疎通。token認証（遠隔は wss）。
   114	- [ ] **設定ダイアログ(歯車)** からサーバWSアドレス/トークン等を変更・`client.toml`保存でき、再接続に反映。
   115	- [ ] サーバ/クライアント各失敗（接続断・サーバ不達・OOM）でクラッシュせず復帰。
   116	
   117	## 12. PLAN.md との関係
   118	- §3 SLO・§7 tomlプロファイル・§5 STT・§8 音声I/O原則は**維持**（配置がサーバ/クライアントに分かれるだけ）。
   119	- §6 並行設計はクライアント(qasync+mic/再生スレッド)とサーバ(asyncio WS + STTワーカースレッド)に分割。
   120	- 旧 M3–M6 は本書 §9 の S1/C1/S2/C2/X1/X2 に再編。

exec
/bin/bash -lc "nl -ba pyproject.toml | sed -n '1,120p'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
     1	[project]
     2	name = "text-to-shaberi"
     3	version = "0.1.0"
     4	description = "Add your description here"
     5	readme = "README.md"
     6	authors = [
     7	    { name = "May_Kirihara", email = "may.kirihara.the.snow+Cre@gmail.com" }
     8	]
     9	requires-python = ">=3.12"
    10	dependencies = [
    11	    "fastapi>=0.136.3",
    12	    "httpx>=0.28.1",
    13	    "numpy>=2.4.4",
    14	    "pyside6>=6.11.1",
    15	    "qasync>=0.28.0",
    16	    "qwen-asr>=0.0.6",
    17	    "silero-vad>=6.2.1",
    18	    "sounddevice>=0.5.5",
    19	    "soxr>=1.1.0",
    20	    "torch>=2.11.0",
    21	    "uvicorn>=0.48.0",
    22	]
    23	
    24	[project.scripts]
    25	text-to-shaberi = "text_to_shaberi:main"
    26	
    27	[[tool.uv.index]]
    28	url = "https://download.pytorch.org/whl/cu128"
    29	
    30	[build-system]
    31	requires = ["uv_build>=0.9.16,<0.10.0"]
    32	build-backend = "uv_build"
    33	
    34	[dependency-groups]
    35	dev = [
    36	    "pytest>=9.0.3",
    37	]

codex
必要な材料は揃いました。レビューでは、計画の方向性は支持しつつ、R0前に仕様へ落としておかないと後段で手戻りになる項目を優先して書きます。
codex
以下、ファイル編集なしの批判的レビューです。

**総合判断**
v0.3 のクライアント-サーバ分離方針は妥当です。GPU常駐処理をLinuxサーバへ寄せ、Windows側をGUI/マイク/再生に限定する判断は、依存・運用・VRAMの観点で筋が良いです。

ただし現状の計画は「配置換え」の粒度がやや楽観的です。特に WebSocket プロトコル、依存境界、stop/cancel、バックプレッシャ、Windows実機音声、Opus導入時期が曖昧なままだと、S1/C1以降で設計を戻す可能性があります。

## 指摘

### 1. [高] §8 移行マップは「捨てず配置換え」だけでは成立しない
既存 `StreamingPlayer` は [audio/player.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/audio/player.py:14) で `tts.client` の `_pcm_to_float32 / bytes_per_sample / split_full_samples` に依存しています。§8 では `tts/client.py` を server、`audio/player.py` を client へ移すため、このままだと Windowsクライアントが server/tts 側に依存します。

修正提案:
`_pcm_to_float32`, `bytes_per_sample`, `split_full_samples`, `AudioFormat` は R0 で必ず `common/audio_format.py` または `common/pcm.py` へ移す、と明記してください。§8 の `tts/profile.py`「一部fmtはcommon」では弱いです。

### 2. [高] 現行 pyproject では Windows client が torch 不要にならない
現状トップレベル依存に `qwen-asr`, `silero-vad`, `torch` が入っています [pyproject.toml](/mnt/holyland/Project/Text-to-shaberi/pyproject.toml:10)。§3 の optional-dependencies 方針自体は正しいですが、R0 の受け入れ基準に「client extras のみで import/起動できる」検証がありません。

修正提案:
R0 に以下を追加してください。

- `dependencies` は common最小限のみにする
- `[project.optional-dependencies] client/server` を定義
- `uv sync --extra client` 環境で `torch`, `qwen_asr` import 不要を確認
- client パッケージ配下で server/STT/TTS を import しない静的チェックを入れる

### 3. [高] WSバイナリフレームの id 関連付けが単一ストリーム前提に依存しすぎ
§4 は「直近 audio_begin に属する」方式です [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:50)。MVPとしては可ですが、STT自動TTSや stop、再接続、エラーを考えると曖昧です。特に server→client の `transcript` 後に自動TTSが始まる場合、`id` が STT id なのか派生 speak id なのか不明です。

修正提案:
MVPでも以下を protocol v1 に入れてください。

- `audio_begin` から `audio_end/error/stopped` までを atomic stream と定義
- バイナリは「現在開いている同方向stream idに属する」と明文化
- 同方向stream中の新規 `audio_begin/stt_begin` は protocol error
- 自動TTSは `parent_id` を持つ別 `id` にする  
  例: `{"type":"audio_begin","id":"tts-u2","parent_id":"u2",...}`
- `error` は `id` nullable ではなく、connection error と stream error を分ける

### 4. [高] stop の意味が広すぎる
§4 では `stop` が TTS/STT生成をキャンセルとあります [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:59)。しかし PLAN v0.2 では再生停止、TTSキャンセル、キュー破棄を区別していました。分離後は「クライアント再生停止」と「サーバ生成停止」が別責務になります。

修正提案:
`stop` を最低2種類に分けてください。

- `stop_playback`: client local。サーバに送らない場合もある
- `cancel`: server stream cancel。`id` 必須
- 将来用に `flush` または `clear_queue`

C1 では停止ボタン押下時に「local abort 即時」＋「server cancel 送信」を要求仕様に入れるべきです。

### 5. [高] バックプレッシャを X2 まで後回しにすると C2 で詰まる
§9 では WS再接続/バックプレッシャが X2 です [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:97)。テキストTTSだけなら許容できますが、C2 のマイク送信では送信過多・ネットワーク揺れ・サーバSTT待ちで詰まります。

修正提案:
C2 前、できれば S2/C2 の受け入れ基準に最低限を入れてください。

- client mic送信キューの最大秒数、最大bytes
- 上限超過時は古い未送信音声を破棄するか録音停止
- server は `stt_begin` 中に次の `stt_begin` を拒否/キュー化
- `ack` または `stt_ready` で発話セグメント単位の流量制御

### 6. [中] ハンドシェイク情報が不足している
`hello`/`ready` は codec だけです [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:45)。実装時には protocol version、max chunk、server feature、profile一覧、auth失敗時の扱いが必要になります。

修正提案:
`hello` に `protocol_version`, `client_name`, `client_id`, `caps` を追加。`ready` に `session_id`, `server_time`, `profiles`, `limits`, `features` を追加してください。

### 7. [中] Opus自動戦略は妥当だが、X1までは PCM 固定にした方がよい
§5 の auto=LANならPCM/遠隔ならOpus は合理的です。ただし Windows の Opus 依存は §10 でもリスク扱いされており、C1/S2/C2 の前に入れると検証軸が増えます。

修正提案:
R0-S2-C2 は `pcm_s16le` 固定。X1で Opus を追加する、と明記してください。受け入れ基準 §11 の「LAN/PCM と Opus 双方」は MVP ではなく X1 acceptance に分離するのが安全です。

### 8. [中] Opus format の定義が足りない
Opus は 48k/20ms とありますが [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:65)、packet framing、mono/stereo、pre-skip、flush、欠損時の扱いがありません。

修正提案:
`AudioFormat` と `CodecFormat` を分けてください。PCMは sample_rate が実音声rate、Opusは codec clock 48k と decoded output rate を別に持つ形が安全です。

### 9. [中] TTFA≤400ms は lazy-start 修正後の preroll を予算に入れるべき
M2レビューで先頭欠けの原因は `OutputStream.start()` 早すぎと判明しています。現行コードも [audio/player.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/audio/player.py:91) で即startしています。§7 の TTFA見積りは再生側preroll 80-150msを含んでいません。

修正提案:
C1 の TTFA定義を分けてください。

- server first audio byte
- client first chunk received
- playback write start
- audible start estimate

lazy-start + preroll 80-150ms を足しても p50≤400ms に収まるか再計算してください。148msなら余裕、270msならタイトです。

### 10. [中] STT→自動TTSのサーバ内連結は早めに決めるべき
§10-6 で未決扱いですが [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:106)、S2実装に直撃します。サーバ内連結にすると低遅延ですが、クライアント側の「文字を確認してから話す」「LLMに渡す」拡張とは違う経路になります。

修正提案:
MVPはサーバ内自動TTSを採用し、`auto_tts: bool` を `stt_begin` または client設定で送る形に固定するのがよいです。クライアント往復TTSは後で optional にしてください。

### 11. [中] セキュリティの既定値が矛盾気味
§6 は LAN token任意、既定は共有トークン1個とあります [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:69)。任意なのか既定必須なのか曖昧です。また Origin チェックはブラウザには有効ですが、Python WSクライアントでは補助的です。

修正提案:
既定を明確化してください。

- bind default は `127.0.0.1`
- LAN公開時は設定で `bind=0.0.0.0` と `token` 必須
- token未設定で外部bindなら起動拒否
- Originはブラウザ対策として実装、主防御は token/TLS
- Authorization header 非対応環境用に hello token を許可するなら優先順位を定義

### 12. [中] 設定UIで TTSプロファイル選択を client 側に置く設計が未定義
TTSプロファイルはサーバ側 `profiles/tts/*.toml` です [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:39)。一方で client設定ダイアログに TTSプロファイル選択があります [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:40)。client がどう一覧取得するかが未定義です。

修正提案:
`ready.profiles` または `profiles` メッセージでサーバから一覧を返す仕様を追加してください。client.toml には `active_profile_id` だけ保存し、存在しない場合は server default にフォールバック。

### 13. [中] Windows VADで torch不要は成立しない可能性が高い
Silero VAD は通常 torch 系依存です。§3 は Windows client が torch不要、§10 は Silero VADのtorch依存をリスクにしています。この2つが衝突しています。

修正提案:
client MVP の VADを次のどちらかに固定してください。

- Windows client は `webrtcvad` をMVP既定、Sileroは optional
- Sileroを使うなら client は torch CPU 依存を許容する

「torch不要」を守るなら webrtcvad 先行が現実的です。

### 14. [低] `api/server.py` の移行記述が現状とずれている可能性
§8 に `api/server.py` がありますが [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:87)、参照コード範囲では現在の中心は `app.py` と `tts/client.py` です。既存にない/未成熟なファイルなら、移行対象として過大評価しない方がよいです。

修正提案:
R0前に `rg --files src/text_to_shaberi` ベースで実在ファイル一覧に合わせ、移行表を「実在資産」「新規実装」「廃止/保留」に分けてください。

### 15. [低] Windows実機リスクの受け入れ基準が抽象的
§10 に WASAPI/VB-CABLE リスクはありますが、X2まで後回しです。C1の再生時点でWindows実機検証を一度入れないと、後段のマイク/VAD/STTと原因が混ざります。

修正提案:
C1 acceptance に Windows 実機で以下を追加してください。

- WASAPI既定出力で再生
- VB-CABLE選択で再生
- デバイス切替後の次発話反映
- stop ≤150ms
- 先頭欠けなし

## マイルストーンへの所見

R0→S1→C1→S2→C2→X1→X2 の順序は概ね良いです。ただし以下は前倒ししてください。

- R0: common PCM/protocol/config分離と client-only install 検証
- S1/C1: stop/cancel、stream状態機械、lazy-start
- S2/C2: 最低限のバックプレッシャ
- C1: Windows再生/VB-CABLEの初回実機検証

X2 に「再接続/バックプレッシャ/Windows実機/エラー処理」をまとめすぎです。X2は仕上げではなく不確実性の山になっています。

## 実装(R0)着手前に必ず反映すべき TOP3

1. **依存境界と common 配置を明文化**
   `AudioFormat`、PCMヘルパー、protocol schema を common へ移し、client-only環境で torch/qwen/server import 不要をR0 acceptanceに入れる。

2. **WebSocket protocol v1 を状態機械として定義**
   `audio_begin` から `audio_end/error/stopped` までのstream所有、id/parent_id、stop/cancel、同時stream禁止、protocol errorを明文化する。

3. **C1/S2/C2前に最低限の流量制御とWindows音声検証を入れる**
   バックプレッシャをX2送りにせず、マイク送信キュー上限・発話単位ack・サーバbusy時挙動を決める。C1でWindows WASAPI/VB-CABLEを単独検証する。
hook: Stop
hook: Stop Completed
tokens used
48,539
以下、ファイル編集なしの批判的レビューです。

**総合判断**
v0.3 のクライアント-サーバ分離方針は妥当です。GPU常駐処理をLinuxサーバへ寄せ、Windows側をGUI/マイク/再生に限定する判断は、依存・運用・VRAMの観点で筋が良いです。

ただし現状の計画は「配置換え」の粒度がやや楽観的です。特に WebSocket プロトコル、依存境界、stop/cancel、バックプレッシャ、Windows実機音声、Opus導入時期が曖昧なままだと、S1/C1以降で設計を戻す可能性があります。

## 指摘

### 1. [高] §8 移行マップは「捨てず配置換え」だけでは成立しない
既存 `StreamingPlayer` は [audio/player.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/audio/player.py:14) で `tts.client` の `_pcm_to_float32 / bytes_per_sample / split_full_samples` に依存しています。§8 では `tts/client.py` を server、`audio/player.py` を client へ移すため、このままだと Windowsクライアントが server/tts 側に依存します。

修正提案:
`_pcm_to_float32`, `bytes_per_sample`, `split_full_samples`, `AudioFormat` は R0 で必ず `common/audio_format.py` または `common/pcm.py` へ移す、と明記してください。§8 の `tts/profile.py`「一部fmtはcommon」では弱いです。

### 2. [高] 現行 pyproject では Windows client が torch 不要にならない
現状トップレベル依存に `qwen-asr`, `silero-vad`, `torch` が入っています [pyproject.toml](/mnt/holyland/Project/Text-to-shaberi/pyproject.toml:10)。§3 の optional-dependencies 方針自体は正しいですが、R0 の受け入れ基準に「client extras のみで import/起動できる」検証がありません。

修正提案:
R0 に以下を追加してください。

- `dependencies` は common最小限のみにする
- `[project.optional-dependencies] client/server` を定義
- `uv sync --extra client` 環境で `torch`, `qwen_asr` import 不要を確認
- client パッケージ配下で server/STT/TTS を import しない静的チェックを入れる

### 3. [高] WSバイナリフレームの id 関連付けが単一ストリーム前提に依存しすぎ
§4 は「直近 audio_begin に属する」方式です [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:50)。MVPとしては可ですが、STT自動TTSや stop、再接続、エラーを考えると曖昧です。特に server→client の `transcript` 後に自動TTSが始まる場合、`id` が STT id なのか派生 speak id なのか不明です。

修正提案:
MVPでも以下を protocol v1 に入れてください。

- `audio_begin` から `audio_end/error/stopped` までを atomic stream と定義
- バイナリは「現在開いている同方向stream idに属する」と明文化
- 同方向stream中の新規 `audio_begin/stt_begin` は protocol error
- 自動TTSは `parent_id` を持つ別 `id` にする  
  例: `{"type":"audio_begin","id":"tts-u2","parent_id":"u2",...}`
- `error` は `id` nullable ではなく、connection error と stream error を分ける

### 4. [高] stop の意味が広すぎる
§4 では `stop` が TTS/STT生成をキャンセルとあります [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:59)。しかし PLAN v0.2 では再生停止、TTSキャンセル、キュー破棄を区別していました。分離後は「クライアント再生停止」と「サーバ生成停止」が別責務になります。

修正提案:
`stop` を最低2種類に分けてください。

- `stop_playback`: client local。サーバに送らない場合もある
- `cancel`: server stream cancel。`id` 必須
- 将来用に `flush` または `clear_queue`

C1 では停止ボタン押下時に「local abort 即時」＋「server cancel 送信」を要求仕様に入れるべきです。

### 5. [高] バックプレッシャを X2 まで後回しにすると C2 で詰まる
§9 では WS再接続/バックプレッシャが X2 です [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:97)。テキストTTSだけなら許容できますが、C2 のマイク送信では送信過多・ネットワーク揺れ・サーバSTT待ちで詰まります。

修正提案:
C2 前、できれば S2/C2 の受け入れ基準に最低限を入れてください。

- client mic送信キューの最大秒数、最大bytes
- 上限超過時は古い未送信音声を破棄するか録音停止
- server は `stt_begin` 中に次の `stt_begin` を拒否/キュー化
- `ack` または `stt_ready` で発話セグメント単位の流量制御

### 6. [中] ハンドシェイク情報が不足している
`hello`/`ready` は codec だけです [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:45)。実装時には protocol version、max chunk、server feature、profile一覧、auth失敗時の扱いが必要になります。

修正提案:
`hello` に `protocol_version`, `client_name`, `client_id`, `caps` を追加。`ready` に `session_id`, `server_time`, `profiles`, `limits`, `features` を追加してください。

### 7. [中] Opus自動戦略は妥当だが、X1までは PCM 固定にした方がよい
§5 の auto=LANならPCM/遠隔ならOpus は合理的です。ただし Windows の Opus 依存は §10 でもリスク扱いされており、C1/S2/C2 の前に入れると検証軸が増えます。

修正提案:
R0-S2-C2 は `pcm_s16le` 固定。X1で Opus を追加する、と明記してください。受け入れ基準 §11 の「LAN/PCM と Opus 双方」は MVP ではなく X1 acceptance に分離するのが安全です。

### 8. [中] Opus format の定義が足りない
Opus は 48k/20ms とありますが [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:65)、packet framing、mono/stereo、pre-skip、flush、欠損時の扱いがありません。

修正提案:
`AudioFormat` と `CodecFormat` を分けてください。PCMは sample_rate が実音声rate、Opusは codec clock 48k と decoded output rate を別に持つ形が安全です。

### 9. [中] TTFA≤400ms は lazy-start 修正後の preroll を予算に入れるべき
M2レビューで先頭欠けの原因は `OutputStream.start()` 早すぎと判明しています。現行コードも [audio/player.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/audio/player.py:91) で即startしています。§7 の TTFA見積りは再生側preroll 80-150msを含んでいません。

修正提案:
C1 の TTFA定義を分けてください。

- server first audio byte
- client first chunk received
- playback write start
- audible start estimate

lazy-start + preroll 80-150ms を足しても p50≤400ms に収まるか再計算してください。148msなら余裕、270msならタイトです。

### 10. [中] STT→自動TTSのサーバ内連結は早めに決めるべき
§10-6 で未決扱いですが [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:106)、S2実装に直撃します。サーバ内連結にすると低遅延ですが、クライアント側の「文字を確認してから話す」「LLMに渡す」拡張とは違う経路になります。

修正提案:
MVPはサーバ内自動TTSを採用し、`auto_tts: bool` を `stt_begin` または client設定で送る形に固定するのがよいです。クライアント往復TTSは後で optional にしてください。

### 11. [中] セキュリティの既定値が矛盾気味
§6 は LAN token任意、既定は共有トークン1個とあります [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:69)。任意なのか既定必須なのか曖昧です。また Origin チェックはブラウザには有効ですが、Python WSクライアントでは補助的です。

修正提案:
既定を明確化してください。

- bind default は `127.0.0.1`
- LAN公開時は設定で `bind=0.0.0.0` と `token` 必須
- token未設定で外部bindなら起動拒否
- Originはブラウザ対策として実装、主防御は token/TLS
- Authorization header 非対応環境用に hello token を許可するなら優先順位を定義

### 12. [中] 設定UIで TTSプロファイル選択を client 側に置く設計が未定義
TTSプロファイルはサーバ側 `profiles/tts/*.toml` です [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:39)。一方で client設定ダイアログに TTSプロファイル選択があります [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:40)。client がどう一覧取得するかが未定義です。

修正提案:
`ready.profiles` または `profiles` メッセージでサーバから一覧を返す仕様を追加してください。client.toml には `active_profile_id` だけ保存し、存在しない場合は server default にフォールバック。

### 13. [中] Windows VADで torch不要は成立しない可能性が高い
Silero VAD は通常 torch 系依存です。§3 は Windows client が torch不要、§10 は Silero VADのtorch依存をリスクにしています。この2つが衝突しています。

修正提案:
client MVP の VADを次のどちらかに固定してください。

- Windows client は `webrtcvad` をMVP既定、Sileroは optional
- Sileroを使うなら client は torch CPU 依存を許容する

「torch不要」を守るなら webrtcvad 先行が現実的です。

### 14. [低] `api/server.py` の移行記述が現状とずれている可能性
§8 に `api/server.py` がありますが [ARCH](/mnt/holyland/Project/Text-to-shaberi/docs/ARCH-v0.3-client-server.md:87)、参照コード範囲では現在の中心は `app.py` と `tts/client.py` です。既存にない/未成熟なファイルなら、移行対象として過大評価しない方がよいです。

修正提案:
R0前に `rg --files src/text_to_shaberi` ベースで実在ファイル一覧に合わせ、移行表を「実在資産」「新規実装」「廃止/保留」に分けてください。

### 15. [低] Windows実機リスクの受け入れ基準が抽象的
§10 に WASAPI/VB-CABLE リスクはありますが、X2まで後回しです。C1の再生時点でWindows実機検証を一度入れないと、後段のマイク/VAD/STTと原因が混ざります。

修正提案:
C1 acceptance に Windows 実機で以下を追加してください。

- WASAPI既定出力で再生
- VB-CABLE選択で再生
- デバイス切替後の次発話反映
- stop ≤150ms
- 先頭欠けなし

## マイルストーンへの所見

R0→S1→C1→S2→C2→X1→X2 の順序は概ね良いです。ただし以下は前倒ししてください。

- R0: common PCM/protocol/config分離と client-only install 検証
- S1/C1: stop/cancel、stream状態機械、lazy-start
- S2/C2: 最低限のバックプレッシャ
- C1: Windows再生/VB-CABLEの初回実機検証

X2 に「再接続/バックプレッシャ/Windows実機/エラー処理」をまとめすぎです。X2は仕上げではなく不確実性の山になっています。

## 実装(R0)着手前に必ず反映すべき TOP3

1. **依存境界と common 配置を明文化**
   `AudioFormat`、PCMヘルパー、protocol schema を common へ移し、client-only環境で torch/qwen/server import 不要をR0 acceptanceに入れる。

2. **WebSocket protocol v1 を状態機械として定義**
   `audio_begin` から `audio_end/error/stopped` までのstream所有、id/parent_id、stop/cancel、同時stream禁止、protocol errorを明文化する。

3. **C1/S2/C2前に最低限の流量制御とWindows音声検証を入れる**
   バックプレッシャをX2送りにせず、マイク送信キュー上限・発話単位ack・サーバbusy時挙動を決める。C1でWindows WASAPI/VB-CABLEを単独検証する。
