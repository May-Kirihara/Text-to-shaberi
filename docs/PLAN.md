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

**性能（§3 SLOと対応・計測表示）**
- [ ] GUI/API入力 TTFA：p50/p95 が目標内
- [ ] マイク往復：p50/p95 が目標内（疑似streaming前提）
- [ ] TTS first-chunk latency / STT処理時間 を計測表示
- [ ] 再生 underrun 回数 ≈ 0
- [ ] 割り込み→無音化 ≤ 150ms

**失敗時動作**
- [ ] TTS/STTサーバ不達・OOM・デバイス喪失で**クラッシュせず**、UIにエラー提示し復帰

---

## 付録：参照（調査ソース）
- GPT-SoVITS `api_v2.py`: https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py
- Qwen3-ASR-1.7B（ローカル/日本語/ストリーミング）: https://huggingface.co/Qwen/Qwen3-ASR-1.7B
- Qwen3-ASR Toolkit / DashScope: https://github.com/QwenLM/Qwen3-ASR-Toolkit
- Qwen Real-Time Speech Recognition (WebSocket): https://www.alibabacloud.com/help/en/model-studio/qwen-real-time-speech-recognition
- codex レビュー全文: `docs/REVIEW-codex.md`
