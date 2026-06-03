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
session id: 019e8b10-d512-74a1-b249-774f75c61210
--------
user
あなたはシニアソフトウェアアーキテクトです。このリポジトリの実装計画書 docs/IMPL-M2.md を批判的にレビューしてください。これは「M2: ストリーミング再生＋音声I/O」の実装計画で、この後この計画をもとに別のLLM(GLM-5.1)が実装します。計画が実装可能で正確か、既存コードと整合するかを厳しく見てください。

参照すべき既存ファイル: docs/PLAN.md(全体計画§3 SLO/§8 音声I/O), src/text_to_shaberi/tts/client.py, src/text_to_shaberi/tts/profile.py, src/text_to_shaberi/audio/player.py, src/text_to_shaberi/audio/devices.py, src/text_to_shaberi/app.py, src/text_to_shaberi/ui/main_window.py, profiles/tts/gpt-sovits.toml

観点:
1. ストリーミング設計の正しさ（httpx aiter_bytes、soxr.ResampleStream(in_rate,out_rate,num_channels,dtype,quality)+resample_chunk(x,last=)でのgaplessリサンプル、PCMサンプル境界のcarry処理、run_in_executorでのブロッキングwriteによるバックプレッシャと順序保証）
2. 既存コードとの整合（_build_request は現状 httpx.Request を返す。計画の stream 実装はこれを stream に流用できるか。_decode_raw/_PCM_DTYPE/_device_samplerate/preferred_default_output との接続）
3. レイテンシ目標（TTFA≤400ms, 狙い270ms）が設計で達成可能か。OutputStream先行open・最初のchunk計測の妥当性。underrun/境界ノイズのリスク
4. 仮想sink(ttsvirt)ルーティング（pactl move-sink-input、sink-input特定、PULSE_PROP application.name）の現実性と確実性、失敗時フォールバック
5. 停止(abort)・例外処理・リソースリーク（stream/clientのclose）
6. 計画の抜け・曖昧さ（GLMが実装で迷う箇所）、変更ファイルの過不足

出力はファイルを編集せず、Markdownのレビュー結果のみ。各指摘に重要度(高/中/低)と具体的な修正提案を付け、最後に総評と「実装委託前に計画へ必ず反映すべきTOP3」を挙げてください。
hook: UserPromptSubmit
hook: UserPromptSubmit
hook: UserPromptSubmit Completed
hook: UserPromptSubmit Completed
codex
実装計画と参照ファイルを読み合わせて、計画上の実装リスクに絞ってレビューします。編集は行いません。
exec
/bin/bash -lc "sed -n '1,260p' docs/IMPL-M2.md" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
# M2 実装計画書: ストリーミング再生 ＋ 音声I/O

> フロー: 本書を codex レビュー → 反映 → 実装は SecondOpinion(GLM-5.1) に委託 → 自分で検証/統合。
> 対象リポジトリ: `/mnt/holyland/Project/Text-to-shaberi`（Python3.12 / uv / PySide6 / qasync）

## 1. 目的・スコープ
M1 の「全受信→一括再生」を **チャンク逐次再生** に置き換え、**TTFA（最初の音まで）を実測 ~270ms** に短縮する（§3 SLO）。あわせて出力デバイス切替の実用化と、仮想sink `TTS_Virtual`(ttsvirt) への配信ルーティングを入れる。

非スコープ: 割り込み/キュー（M3）、マイク/STT（M4/M5）。ただし停止ボタンと `abort()` は M3 の素地として用意する。

## 2. 現状(M1)の関連実装
- `tts/client.py`: `TTSClient.synthesize(profile, text) -> AudioBuffer`（全受信→デコード）。`_decode_raw/_decode_wav`、`_PCM_DTYPE`。
- `audio/player.py`: `play(samples, sample_rate, device)`（出力rateへ soxr 一括変換→`sd.play`/`sd.wait`）。`_device_samplerate`、`stop()`。
- `audio/devices.py`: `preferred_default_output()`（None→"pipewire"等）、`list_output_devices()`。
- `ui/main_window.py`: 出力先 combo（"既定デバイス（システム既定sink）"=None ＋ 各デバイス index）、送信→`on_speak(text)`。
- `app.py: run_gui`: `speak(text)` が `synthesize`→`run_in_executor(play)`。
- プロファイル実値: `response.mode=audio_stream`, `audio.encoding=pcm_s16le`, `sample_rate=32000`, `channels=1`, `request.body.streaming_mode=3`, `media_type=raw`。GPT-SoVITS api_v2 は streaming で raw PCM チャンクを返す。

## 3. 設計

### 3.1 ストリーミングTTSクライアント（`tts/client.py`）
- 追加: `async def stream(self, profile, text, **variables) -> AsyncIterator[bytes]`
  - 適用条件: `profile.response_mode == "audio_stream"`。それ以外は呼び出し側が `synthesize` にフォールバック。
  - リクエスト構築は `_build_request` を再利用（GET=params / json / form）。
  - 実装:
    ```python
    request = self._build_request(profile, profile.render_body(text, **variables))
    async with self._client.stream(request.method, request.url, content=request.content,
                                   headers=request.headers, timeout=profile.timeout_sec) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
    ```
    （`_build_request` が作る httpx.Request から method/url/headers/content を取り出して `stream` する。json/params を二重に組まないよう、`_build_request` を「リクエスト引数 dict を返す」形にリファクタしても良い。）
- PCMデコードを共通化: `_pcm_to_float32(data: bytes, encoding: str, channels: int) -> np.ndarray` を新設し、`_decode_raw` と StreamingPlayer から共用。

### 3.2 ストリーミング再生（`audio/player.py`）
- 追加: `class StreamingPlayer`
  - 役割: 受信PCMバイト列を逐次受け取り、出力デバイスrateへ **gaplessリサンプル**して `sd.OutputStream` に書き込む。
  - コンストラクタ `__init__(self, src_rate:int, channels:int=1, device:int|None=None, encoding:str="pcm_s16le")`:
    - `device is None` → `preferred_default_output()` で解決。
    - `target_sr = _device_samplerate(sd, device)`。
    - rate差があれば `self._resampler = soxr.ResampleStream(src_rate, target_sr, channels, dtype="float32", quality="HQ")`、無ければ None。
    - `self._stream = sd.OutputStream(samplerate=target_sr, channels=channels, dtype="float32", device=device)`; `self._stream.start()`。
    - `self._carry = b""`（PCMサンプル境界を跨ぐ端数バイト保持）。
  - `write_bytes(self, data: bytes) -> None`:
    - `data = self._carry + data`; `step = bytes_per_sample(encoding) * channels`; `n = (len(data)//step)*step`; `self._carry = data[n:]`。
    - `frames = _pcm_to_float32(data[:n], encoding, channels)`（mono は 1D、多ch は (-1,channels)）。
    - `if self._resampler: frames = self._resampler.resample_chunk(frames)`。
    - `if len(frames): self._stream.write(frames)`（ブロッキング＝自然なバックプレッシャ）。
  - `close(self) -> None`:
    - resampler があれば `tail = self._resampler.resample_chunk(<空配列>, last=True)` を flush して write。
    - `self._stream.stop(); self._stream.close()`。
  - `abort(self) -> None`: `self._stream.abort(); self._stream.close()`（即停止・M3割り込み用）。
  - soxr API（確認済）: `ResampleStream(in_rate, out_rate, num_channels, dtype='float32', quality='HQ')`、`resample_chunk(x, last=False)`。mono入力は 1D ndarray 可。空配列+`last=True` で残差flush。
- `play()`(M1) は wav/json や非stream用に**据え置き**。

### 3.3 app.py 統合（`run_gui` の `speak`）
- 経路選択: `use_stream = (profile.response_mode == "audio_stream")`。
- streaming 経路:
  ```python
  device = window.selected_output_device()
  player = await loop.run_in_executor(None, lambda: StreamingPlayer(
      profile.audio.sample_rate, profile.audio.channels, device, profile.audio.encoding))
  current_player[0] = player  # 停止ボタン用に保持
  t0 = loop.time(); first = None
  try:
      async for chunk in client.stream(profile, text):
          if first is None:
              first = loop.time() - t0
              window.log(f"[tts] first-chunk {first*1000:.0f}ms")
          await loop.run_in_executor(None, player.write_bytes, chunk)
      await loop.run_in_executor(None, player.close)
      window.log(f"[tts] 再生完了 {loop.time()-t0:.2f}s")
  except Exception as exc:
      await loop.run_in_executor(None, player.abort)
      window.log(f"[error] {exc!r}")
  finally:
      current_player[0] = None
  ```
- 非stream時は従来の `synthesize`→`play` フォールバックを残す。
- `client` の close は既存 `app.aboutToQuit` 処理を維持。

### 3.4 出力デバイス切替の実用化
- 各発話で `window.selected_output_device()` を都度参照（既存）。ストリームは発話毎に open/close するため、**切替は次発話から反映**（再生中の動的切替は M3 と統合）。
- combo 変更時に再生中なら「次の発話から切替わります」をログ表示（任意）。

### 3.5 停止ボタン（M3素地）
- `ui/main_window.py`: 「停止」ボタン追加。`set_on_stop(callback)` を用意し、`app` 側で `current_player[0].abort()` を `run_in_executor` で実行。
- 再生中でなければ no-op。

### 3.6 仮想sink `TTS_Virtual` ルーティング
- 課題: PortAudio(ALSA) は PipeWire 個別sink名を列挙しない。`pipewire` デバイスで再生すると **システム既定sink** へ流れる。
- 方針(M2, best-effort): combo に項目「TTS_Virtual（配信用）」を追加（data はセンチネル文字列 `"__ttsvirt__"`）。選択時:
  1. 再生は **pipewireデバイス**（`preferred_default_output()`）で開く。
  2. ストリーム開始直後に、PipeWire(pulse) の sink-input を `pactl` で `ttsvirt` へ移動:
     - `pactl list sink-inputs` から自プロセス由来の sink-input を特定（`application.process.id` がアプリPID、または `application.name`）。
     - `pactl move-sink-input <id> ttsvirt`。
  3. アプリ名を固定するため、PortAudioストリーム生成前に環境変数で pulse application.name を設定（例 `PULSE_PROP="application.name=text-to-shaberi"` を起動時 os.environ に）。
- 特定が不確実な場合はログに警告し既定sink再生に留める（クラッシュさせない）。確実版（`pw-link`/`node.target`）は **M2b** で詰める。
- `audio/routing.py`(新規, 任意) に `move_app_to_sink(sink="ttsvirt", pid=os.getpid())` を分離実装。

## 4. 受け入れ基準
- [ ] 既定デバイスで streaming 再生でき、`first-chunk` をログ表示、**TTFA ≤ 400ms**（狙い ~270ms）。
- [ ] underrun/ノイズ無く最後まで再生（resampler境界含む）。
- [ ] 出力先デバイス変更が次発話で反映。
- [ ] 「停止」で再生中の音が ~150ms 程度で止まる。
- [ ] 「TTS_Virtual」選択で OBS/Discord の「Monitor of TTS_Virtual」に音が流れる（best-effort・失敗時は既定sink＋警告ログ）。
- [ ] いずれの失敗もクラッシュせず UI にエラー表示。

## 5. 変更ファイル
- `tts/client.py`（`stream` 追加、`_pcm_to_float32` 共通化、`_build_request` 整理）
- `audio/player.py`（`StreamingPlayer` 追加、`_pcm_to_float32` 利用）
- `audio/routing.py`（新規・任意：pactl による sink-input 移動）
- `ui/main_window.py`（停止ボタン、`set_on_stop`、TTS_Virtual項目）
- `app.py`（streaming統合、停止結線、起動時 PULSE_PROP 設定、仮想sink移動呼び出し）

## 6. テスト
- ヘッドレス（GPT-SoVITSサーバ :9880 起動前提）:
  - `client.stream` で first-chunk 時刻を計測（実測 ~270ms を確認）。
  - `StreamingPlayer` に無音/正弦PCMを write→close して例外無し（`QT_QPA`/audio は XDG_RUNTIME_DIR=/run/user/1000）。
  - `py_compile` 全モジュール。
- GUI（ユーザー確認）: 実音・TTFA表示・デバイス切替・停止・TTS_Virtual→OBS。

## 7. 留意点 / リスク
- `_build_request` は現在 httpx.Request を返す。`stream` 用に method/url/headers/content を取り回す（json は `request.content` に乗る）。二重構築を避けるため、内部を「kwargs を返す」関数に小リファクタしてよい。
- `run_in_executor` の write は順序保証のため **逐次 await**（並列化しない）。
- soxr のmono入力は 1D。`channels>1` のとき (-1,channels) を厳守。
- 仮想sinkルーティングは環境依存で最も不確実 → best-effort＋ログ、確実版は M2b。

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
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/tts/profile.py" in /mnt/holyland/Project/Text-to-shaberi
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
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/tts/client.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""toml駆動の汎用HTTP TTSドライバ（M1: 同期取得）。

プロファイルの request 仕様で TTS-API を叩き、response.mode と audio フォーマットに
従って音声を numpy float32 [-1,1] へデコードする。M1 はレスポンス全体を受け取ってから
返す（チャンク逐次再生は M2）。
"""
from __future__ import annotations

import base64
import io
import wave
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from .profile import AudioFormat, TTSProfile

# encoding 名 → numpy dtype（raw/PCM 用）
_PCM_DTYPE: dict[str, str] = {
    "pcm_s16le": "<i2",
    "pcm_s24le": "<i4",  # 近似（24bitは後続で厳密対応）
    "pcm_s32le": "<i4",
    "pcm_f32le": "<f4",
}
_PCM_FULLSCALE: dict[str, float] = {
    "<i2": 32768.0,
    "<i4": 2147483648.0,
}


@dataclass(slots=True)
class AudioBuffer:
    samples: np.ndarray  # shape (frames, channels), float32 [-1,1]
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate if self.sample_rate else 0.0


def _decode_raw(data: bytes, fmt: AudioFormat) -> AudioBuffer:
    dtype = _PCM_DTYPE.get(fmt.encoding, "<i2")
    arr = np.frombuffer(data, dtype=dtype)
    if dtype.endswith("f4"):
        samples = arr.astype(np.float32)
    else:
        samples = arr.astype(np.float32) / _PCM_FULLSCALE.get(dtype, 32768.0)
    if fmt.channels > 1:
        samples = samples.reshape(-1, fmt.channels)
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

    def _build_request(self, profile: TTSProfile, body: dict[str, Any]) -> httpx.Request:
        kwargs: dict[str, Any] = {"headers": profile.headers, "timeout": profile.timeout_sec}
        if profile.method.upper() == "GET":
            kwargs["params"] = body
        elif profile.body_type == "json":
            kwargs["json"] = body
        elif profile.body_type in ("form", "multipart"):
            kwargs["data"] = body
        else:  # raw_template 等は将来対応
            kwargs["json"] = body
        return self._client.build_request(profile.method, profile.url, **kwargs)

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
        request = self._build_request(profile, body)
        resp = await self._client.send(request)
        resp.raise_for_status()
        return self._decode(profile, resp)


def _extract(payload: dict, profile: TTSProfile) -> str:
    """response.extract.path（ドット区切り）で JSON からフィールドを取り出す（json系用）。"""
    # M1ではプロファイルにextract定義が無いので素朴に "audio" を探す。M6で schema 化。
    if "audio" in payload:
        return payload["audio"]
    raise ValueError("json_base64 のフィールド抽出に失敗（extract.path 未対応）")

codex
計画書とPLANの該当箇所は確認しました。次に再生・デバイス・GUI統合の現状を見て、実装計画がそのまま刺さるかを確認します。
exec
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/audio/player.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""音声再生（M1: ブロッキング再生＋出力rateへの soxr リサンプル）。

出力デバイスは TTS の出力rate（例 32000Hz）を受け付けないことがある（生のALSA hwデバイス等は
44100/48000固定）。そのため**出力デバイスの既定rateへ soxr で変換**してから再生する。
チャンク逐次再生（OutputStream）と明示的なデバイス切替(drain/stop/reopen)は M2 で拡張する。
"""
from __future__ import annotations

import numpy as np


def _device_samplerate(sd, device: int | None) -> int:
    info = sd.query_devices(kind="output") if device is None else sd.query_devices(device)
    return int(info["default_samplerate"])


def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    """samples(float32 [-1,1]) を device で再生し、完了まで待つ（ブロッキング）。

    出力デバイスの既定rateへ必要に応じてリサンプルする。別スレッド(executor)から呼ぶ前提。
    """
    import sounddevice as sd  # 遅延import（libportaudio2 が必要）

    if device is None:
        # PortAudioのdefaultは無音hwを指すことがあるため、PipeWire/pulse経由（=システム既定sink）を優先
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

exec
/bin/bash -lc "sed -n '1,260p' src/text_to_shaberi/audio/devices.py" in /mnt/holyland/Project/Text-to-shaberi
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
/bin/bash -lc "sed -n '1,320p' src/text_to_shaberi/app.py" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
"""エントリポイント（M0: PySide6 + qasync の足場）。

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
            # -d=daemonize, -r=replace。-x(XIM)は付けない＝fcitx5のXIMと干渉させない
            subprocess.Popen([ibus_daemon, "-dr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(12):  # デーモン起動を最大~1.8s待つ
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

    from .audio.player import play as play_audio
    from .tts.client import TTSClient
    from .ui.main_window import MainWindow

    cfg = load_config(config_path)
    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)

    _setup_input_method(cfg)  # QApplication 生成前に IME を準備

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    client = TTSClient()
    window = MainWindow(cfg, output_devices=_load_output_devices())

    async def speak(text: str) -> None:
        """送信テキスト → TTS合成 → 再生（M1: 同期再生）。"""
        try:
            t0 = loop.time()
            buf = await client.synthesize(profile, text)
            window.log(f"[tts] {buf.duration:.1f}s 音声を {loop.time() - t0:.2f}s で取得 → 再生")
            device = window.selected_output_device()
            await loop.run_in_executor(None, play_audio, buf.samples, buf.sample_rate, device)
        except Exception as exc:  # noqa: BLE001 - UIに見せて継続
            window.log(f"[error] {exc!r}")

    window.set_on_speak(lambda text: asyncio.ensure_future(speak(text)))
    # アプリ終了時に httpx クライアントを閉じる（loop稼働中に実行）
    app.aboutToQuit.connect(lambda: asyncio.ensure_future(client.aclose()))
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
/bin/bash -lc "sed -n '1,220p' profiles/tts/gpt-sovits.toml" in /mnt/holyland/Project/Text-to-shaberi
exec
/bin/bash -lc "sed -n '1,360p' src/text_to_shaberi/ui/main_window.py" in /mnt/holyland/Project/Text-to-shaberi
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

 succeeded in 0ms:
"""メインウィンドウ（M0: 足場）。

テキスト入力・送信・出力デバイス選択・ログ表示の最小UI。
実際のTTSパイプライン接続は M1 以降で `on_speak` に結線する。
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

        self.setWindowTitle("Text-to-shaberi")
        self.resize(680, 460)

        central = QWidget()
        layout = QVBoxLayout(central)

        # 出力デバイス選択（既定 / 仮想オーディオ等）
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("出力先:"))
        self.device_combo = QComboBox()
        self.device_combo.addItem("既定デバイス（システム既定sink）", None)
        for dev in output_devices or []:
            self.device_combo.addItem(f"{dev.name}", dev.index)
        device_row.addWidget(self.device_combo, 1)
        layout.addLayout(device_row)

        # テキスト入力 + 送信
        input_row = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("読み上げるテキストを入力…")
        self.send_button = QPushButton("送信")
        input_row.addWidget(self.text_input, 1)
        input_row.addWidget(self.send_button)
        layout.addLayout(input_row)

        # ログ
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

        self.setCentralWidget(central)

        self.send_button.clicked.connect(self._handle_send)
        self.text_input.returnPressed.connect(self._handle_send)

        self.log(f"profile = {config.active_tts_profile} / stt = {config.stt.backend}")

    def set_on_speak(self, callback: Callable[[str], None]) -> None:
        self._on_speak = callback

    def selected_output_device(self) -> int | None:
        return self.device_combo.currentData()

    def _handle_send(self) -> None:
        text = self.text_input.text().strip()
        if not text:
            return
        self.text_input.clear()
        self.log(f"[gui] {text}")
        if self._on_speak is not None:
            self._on_speak(text)

    def log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

codex
現状コードを見る限り、計画の大筋は既存構造に乗せられますが、HTTPストリーム化、PCM境界、停止時のHTTPキャンセル、仮想sink特定に実装委託前に潰すべき曖昧さがあります。ファイル位置付きで指摘できるよう、該当行も確認します。
exec
/bin/bash -lc "nl -ba src/text_to_shaberi/app.py | sed -n '80,180p'" in /mnt/holyland/Project/Text-to-shaberi
exec
/bin/bash -lc "nl -ba docs/IMPL-M2.md | sed -n '1,240p'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
    80	        if not running:
    81	            # -d=daemonize, -r=replace。-x(XIM)は付けない＝fcitx5のXIMと干渉させない
    82	            subprocess.Popen([ibus_daemon, "-dr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    83	            for _ in range(12):  # デーモン起動を最大~1.8s待つ
    84	                time.sleep(0.15)
    85	                if subprocess.run(["pgrep", "-x", "ibus-daemon"], capture_output=True).returncode == 0:
    86	                    break
    87	            print("[ime] ibus-daemon を起動しました")
    88	
    89	        os.environ["QT_IM_MODULE"] = "ibus"
    90	        os.environ["XMODIFIERS"] = "@im=ibus"
    91	
    92	        ibus_cli = shutil.which("ibus")
    93	        if ibus_cli:
    94	            for engine in ("mozc-jp", "com.google.IBus.Mozc"):
    95	                if subprocess.run([ibus_cli, "engine", engine], capture_output=True).returncode == 0:
    96	                    print(f"[ime] ibusエンジン: {engine}")
    97	                    break
    98	        print("[ime] 日本語入力=ibus+mozc（トグルキーはmozc/ibus設定に従う）")
    99	    except Exception as exc:  # noqa: BLE001 - IME準備の失敗で起動を止めない
   100	        print(f"[ime] ibusセットアップ失敗（英数のみで継続）: {exc}", file=sys.stderr)
   101	
   102	
   103	def run_gui(config_path: str | Path) -> None:
   104	    import qasync
   105	    from PySide6.QtWidgets import QApplication
   106	
   107	    from .audio.player import play as play_audio
   108	    from .tts.client import TTSClient
   109	    from .ui.main_window import MainWindow
   110	
   111	    cfg = load_config(config_path)
   112	    profile = load_profile(cfg.profiles_dir, cfg.active_tts_profile)
   113	
   114	    _setup_input_method(cfg)  # QApplication 生成前に IME を準備
   115	
   116	    app = QApplication(sys.argv)
   117	    loop = qasync.QEventLoop(app)
   118	    asyncio.set_event_loop(loop)
   119	
   120	    client = TTSClient()
   121	    window = MainWindow(cfg, output_devices=_load_output_devices())
   122	
   123	    async def speak(text: str) -> None:
   124	        """送信テキスト → TTS合成 → 再生（M1: 同期再生）。"""
   125	        try:
   126	            t0 = loop.time()
   127	            buf = await client.synthesize(profile, text)
   128	            window.log(f"[tts] {buf.duration:.1f}s 音声を {loop.time() - t0:.2f}s で取得 → 再生")
   129	            device = window.selected_output_device()
   130	            await loop.run_in_executor(None, play_audio, buf.samples, buf.sample_rate, device)
   131	        except Exception as exc:  # noqa: BLE001 - UIに見せて継続
   132	            window.log(f"[error] {exc!r}")
   133	
   134	    window.set_on_speak(lambda text: asyncio.ensure_future(speak(text)))
   135	    # アプリ終了時に httpx クライアントを閉じる（loop稼働中に実行）
   136	    app.aboutToQuit.connect(lambda: asyncio.ensure_future(client.aclose()))
   137	    window.show()
   138	
   139	    with loop:
   140	        loop.run_forever()
   141	
   142	
   143	def main() -> None:
   144	    parser = argparse.ArgumentParser(prog="text-to-shaberi")
   145	    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="config.toml のパス")
   146	    parser.add_argument(
   147	        "--check",
   148	        action="store_true",
   149	        help="GUIを起動せず設定/プロファイル/デバイスのロードを検査して終了",
   150	    )
   151	    args = parser.parse_args()
   152	
   153	    if args.check:
   154	        run_check(args.config)
   155	    else:
   156	        run_gui(args.config)

exec
/bin/bash -lc "nl -ba src/text_to_shaberi/tts/client.py | sed -n '1,220p'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
     1	# M2 実装計画書: ストリーミング再生 ＋ 音声I/O
     2	
     3	> フロー: 本書を codex レビュー → 反映 → 実装は SecondOpinion(GLM-5.1) に委託 → 自分で検証/統合。
     4	> 対象リポジトリ: `/mnt/holyland/Project/Text-to-shaberi`（Python3.12 / uv / PySide6 / qasync）
     5	
     6	## 1. 目的・スコープ
     7	M1 の「全受信→一括再生」を **チャンク逐次再生** に置き換え、**TTFA（最初の音まで）を実測 ~270ms** に短縮する（§3 SLO）。あわせて出力デバイス切替の実用化と、仮想sink `TTS_Virtual`(ttsvirt) への配信ルーティングを入れる。
     8	
     9	非スコープ: 割り込み/キュー（M3）、マイク/STT（M4/M5）。ただし停止ボタンと `abort()` は M3 の素地として用意する。
    10	
    11	## 2. 現状(M1)の関連実装
    12	- `tts/client.py`: `TTSClient.synthesize(profile, text) -> AudioBuffer`（全受信→デコード）。`_decode_raw/_decode_wav`、`_PCM_DTYPE`。
    13	- `audio/player.py`: `play(samples, sample_rate, device)`（出力rateへ soxr 一括変換→`sd.play`/`sd.wait`）。`_device_samplerate`、`stop()`。
    14	- `audio/devices.py`: `preferred_default_output()`（None→"pipewire"等）、`list_output_devices()`。
    15	- `ui/main_window.py`: 出力先 combo（"既定デバイス（システム既定sink）"=None ＋ 各デバイス index）、送信→`on_speak(text)`。
    16	- `app.py: run_gui`: `speak(text)` が `synthesize`→`run_in_executor(play)`。
    17	- プロファイル実値: `response.mode=audio_stream`, `audio.encoding=pcm_s16le`, `sample_rate=32000`, `channels=1`, `request.body.streaming_mode=3`, `media_type=raw`。GPT-SoVITS api_v2 は streaming で raw PCM チャンクを返す。
    18	
    19	## 3. 設計
    20	
    21	### 3.1 ストリーミングTTSクライアント（`tts/client.py`）
    22	- 追加: `async def stream(self, profile, text, **variables) -> AsyncIterator[bytes]`
    23	  - 適用条件: `profile.response_mode == "audio_stream"`。それ以外は呼び出し側が `synthesize` にフォールバック。
    24	  - リクエスト構築は `_build_request` を再利用（GET=params / json / form）。
    25	  - 実装:
    26	    ```python
    27	    request = self._build_request(profile, profile.render_body(text, **variables))
    28	    async with self._client.stream(request.method, request.url, content=request.content,
    29	                                   headers=request.headers, timeout=profile.timeout_sec) as resp:
    30	        resp.raise_for_status()
    31	        async for chunk in resp.aiter_bytes():
    32	            if chunk:
    33	                yield chunk
    34	    ```
    35	    （`_build_request` が作る httpx.Request から method/url/headers/content を取り出して `stream` する。json/params を二重に組まないよう、`_build_request` を「リクエスト引数 dict を返す」形にリファクタしても良い。）
    36	- PCMデコードを共通化: `_pcm_to_float32(data: bytes, encoding: str, channels: int) -> np.ndarray` を新設し、`_decode_raw` と StreamingPlayer から共用。
    37	
    38	### 3.2 ストリーミング再生（`audio/player.py`）
    39	- 追加: `class StreamingPlayer`
    40	  - 役割: 受信PCMバイト列を逐次受け取り、出力デバイスrateへ **gaplessリサンプル**して `sd.OutputStream` に書き込む。
    41	  - コンストラクタ `__init__(self, src_rate:int, channels:int=1, device:int|None=None, encoding:str="pcm_s16le")`:
    42	    - `device is None` → `preferred_default_output()` で解決。
    43	    - `target_sr = _device_samplerate(sd, device)`。
    44	    - rate差があれば `self._resampler = soxr.ResampleStream(src_rate, target_sr, channels, dtype="float32", quality="HQ")`、無ければ None。
    45	    - `self._stream = sd.OutputStream(samplerate=target_sr, channels=channels, dtype="float32", device=device)`; `self._stream.start()`。
    46	    - `self._carry = b""`（PCMサンプル境界を跨ぐ端数バイト保持）。
    47	  - `write_bytes(self, data: bytes) -> None`:
    48	    - `data = self._carry + data`; `step = bytes_per_sample(encoding) * channels`; `n = (len(data)//step)*step`; `self._carry = data[n:]`。
    49	    - `frames = _pcm_to_float32(data[:n], encoding, channels)`（mono は 1D、多ch は (-1,channels)）。
    50	    - `if self._resampler: frames = self._resampler.resample_chunk(frames)`。
    51	    - `if len(frames): self._stream.write(frames)`（ブロッキング＝自然なバックプレッシャ）。
    52	  - `close(self) -> None`:
    53	    - resampler があれば `tail = self._resampler.resample_chunk(<空配列>, last=True)` を flush して write。
    54	    - `self._stream.stop(); self._stream.close()`。
    55	  - `abort(self) -> None`: `self._stream.abort(); self._stream.close()`（即停止・M3割り込み用）。
    56	  - soxr API（確認済）: `ResampleStream(in_rate, out_rate, num_channels, dtype='float32', quality='HQ')`、`resample_chunk(x, last=False)`。mono入力は 1D ndarray 可。空配列+`last=True` で残差flush。
    57	- `play()`(M1) は wav/json や非stream用に**据え置き**。
    58	
    59	### 3.3 app.py 統合（`run_gui` の `speak`）
    60	- 経路選択: `use_stream = (profile.response_mode == "audio_stream")`。
    61	- streaming 経路:
    62	  ```python
    63	  device = window.selected_output_device()
    64	  player = await loop.run_in_executor(None, lambda: StreamingPlayer(
    65	      profile.audio.sample_rate, profile.audio.channels, device, profile.audio.encoding))
    66	  current_player[0] = player  # 停止ボタン用に保持
    67	  t0 = loop.time(); first = None
    68	  try:
    69	      async for chunk in client.stream(profile, text):
    70	          if first is None:
    71	              first = loop.time() - t0
    72	              window.log(f"[tts] first-chunk {first*1000:.0f}ms")
    73	          await loop.run_in_executor(None, player.write_bytes, chunk)
    74	      await loop.run_in_executor(None, player.close)
    75	      window.log(f"[tts] 再生完了 {loop.time()-t0:.2f}s")
    76	  except Exception as exc:
    77	      await loop.run_in_executor(None, player.abort)
    78	      window.log(f"[error] {exc!r}")
    79	  finally:
    80	      current_player[0] = None
    81	  ```
    82	- 非stream時は従来の `synthesize`→`play` フォールバックを残す。
    83	- `client` の close は既存 `app.aboutToQuit` 処理を維持。
    84	
    85	### 3.4 出力デバイス切替の実用化
    86	- 各発話で `window.selected_output_device()` を都度参照（既存）。ストリームは発話毎に open/close するため、**切替は次発話から反映**（再生中の動的切替は M3 と統合）。
    87	- combo 変更時に再生中なら「次の発話から切替わります」をログ表示（任意）。
    88	
    89	### 3.5 停止ボタン（M3素地）
    90	- `ui/main_window.py`: 「停止」ボタン追加。`set_on_stop(callback)` を用意し、`app` 側で `current_player[0].abort()` を `run_in_executor` で実行。
    91	- 再生中でなければ no-op。
    92	
    93	### 3.6 仮想sink `TTS_Virtual` ルーティング
    94	- 課題: PortAudio(ALSA) は PipeWire 個別sink名を列挙しない。`pipewire` デバイスで再生すると **システム既定sink** へ流れる。
    95	- 方針(M2, best-effort): combo に項目「TTS_Virtual（配信用）」を追加（data はセンチネル文字列 `"__ttsvirt__"`）。選択時:
    96	  1. 再生は **pipewireデバイス**（`preferred_default_output()`）で開く。
    97	  2. ストリーム開始直後に、PipeWire(pulse) の sink-input を `pactl` で `ttsvirt` へ移動:
    98	     - `pactl list sink-inputs` から自プロセス由来の sink-input を特定（`application.process.id` がアプリPID、または `application.name`）。
    99	     - `pactl move-sink-input <id> ttsvirt`。
   100	  3. アプリ名を固定するため、PortAudioストリーム生成前に環境変数で pulse application.name を設定（例 `PULSE_PROP="application.name=text-to-shaberi"` を起動時 os.environ に）。
   101	- 特定が不確実な場合はログに警告し既定sink再生に留める（クラッシュさせない）。確実版（`pw-link`/`node.target`）は **M2b** で詰める。
   102	- `audio/routing.py`(新規, 任意) に `move_app_to_sink(sink="ttsvirt", pid=os.getpid())` を分離実装。
   103	
   104	## 4. 受け入れ基準
   105	- [ ] 既定デバイスで streaming 再生でき、`first-chunk` をログ表示、**TTFA ≤ 400ms**（狙い ~270ms）。
   106	- [ ] underrun/ノイズ無く最後まで再生（resampler境界含む）。
   107	- [ ] 出力先デバイス変更が次発話で反映。
   108	- [ ] 「停止」で再生中の音が ~150ms 程度で止まる。
   109	- [ ] 「TTS_Virtual」選択で OBS/Discord の「Monitor of TTS_Virtual」に音が流れる（best-effort・失敗時は既定sink＋警告ログ）。
   110	- [ ] いずれの失敗もクラッシュせず UI にエラー表示。
   111	
   112	## 5. 変更ファイル
   113	- `tts/client.py`（`stream` 追加、`_pcm_to_float32` 共通化、`_build_request` 整理）
   114	- `audio/player.py`（`StreamingPlayer` 追加、`_pcm_to_float32` 利用）
   115	- `audio/routing.py`（新規・任意：pactl による sink-input 移動）
   116	- `ui/main_window.py`（停止ボタン、`set_on_stop`、TTS_Virtual項目）
   117	- `app.py`（streaming統合、停止結線、起動時 PULSE_PROP 設定、仮想sink移動呼び出し）
   118	
   119	## 6. テスト
   120	- ヘッドレス（GPT-SoVITSサーバ :9880 起動前提）:
   121	  - `client.stream` で first-chunk 時刻を計測（実測 ~270ms を確認）。
   122	  - `StreamingPlayer` に無音/正弦PCMを write→close して例外無し（`QT_QPA`/audio は XDG_RUNTIME_DIR=/run/user/1000）。
   123	  - `py_compile` 全モジュール。
   124	- GUI（ユーザー確認）: 実音・TTFA表示・デバイス切替・停止・TTS_Virtual→OBS。
   125	
   126	## 7. 留意点 / リスク
   127	- `_build_request` は現在 httpx.Request を返す。`stream` 用に method/url/headers/content を取り回す（json は `request.content` に乗る）。二重構築を避けるため、内部を「kwargs を返す」関数に小リファクタしてよい。
   128	- `run_in_executor` の write は順序保証のため **逐次 await**（並列化しない）。
   129	- soxr のmono入力は 1D。`channels>1` のとき (-1,channels) を厳守。
   130	- 仮想sinkルーティングは環境依存で最も不確実 → best-effort＋ログ、確実版は M2b。

 succeeded in 0ms:
     1	"""toml駆動の汎用HTTP TTSドライバ（M1: 同期取得）。
     2	
     3	プロファイルの request 仕様で TTS-API を叩き、response.mode と audio フォーマットに
     4	従って音声を numpy float32 [-1,1] へデコードする。M1 はレスポンス全体を受け取ってから
     5	返す（チャンク逐次再生は M2）。
     6	"""
     7	from __future__ import annotations
     8	
     9	import base64
    10	import io
    11	import wave
    12	from dataclasses import dataclass
    13	from typing import Any
    14	
    15	import httpx
    16	import numpy as np
    17	
    18	from .profile import AudioFormat, TTSProfile
    19	
    20	# encoding 名 → numpy dtype（raw/PCM 用）
    21	_PCM_DTYPE: dict[str, str] = {
    22	    "pcm_s16le": "<i2",
    23	    "pcm_s24le": "<i4",  # 近似（24bitは後続で厳密対応）
    24	    "pcm_s32le": "<i4",
    25	    "pcm_f32le": "<f4",
    26	}
    27	_PCM_FULLSCALE: dict[str, float] = {
    28	    "<i2": 32768.0,
    29	    "<i4": 2147483648.0,
    30	}
    31	
    32	
    33	@dataclass(slots=True)
    34	class AudioBuffer:
    35	    samples: np.ndarray  # shape (frames, channels), float32 [-1,1]
    36	    sample_rate: int
    37	
    38	    @property
    39	    def duration(self) -> float:
    40	        return len(self.samples) / self.sample_rate if self.sample_rate else 0.0
    41	
    42	
    43	def _decode_raw(data: bytes, fmt: AudioFormat) -> AudioBuffer:
    44	    dtype = _PCM_DTYPE.get(fmt.encoding, "<i2")
    45	    arr = np.frombuffer(data, dtype=dtype)
    46	    if dtype.endswith("f4"):
    47	        samples = arr.astype(np.float32)
    48	    else:
    49	        samples = arr.astype(np.float32) / _PCM_FULLSCALE.get(dtype, 32768.0)
    50	    if fmt.channels > 1:
    51	        samples = samples.reshape(-1, fmt.channels)
    52	    return AudioBuffer(samples=samples, sample_rate=fmt.sample_rate)
    53	
    54	
    55	def _decode_wav(data: bytes) -> AudioBuffer:
    56	    with wave.open(io.BytesIO(data), "rb") as wf:
    57	        channels = wf.getnchannels()
    58	        width = wf.getsampwidth()
    59	        rate = wf.getframerate()
    60	        frames = wf.readframes(wf.getnframes())
    61	    if width == 2:
    62	        arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    63	    elif width == 4:
    64	        arr = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    65	    elif width == 1:
    66	        arr = (np.frombuffer(frames, dtype="u1").astype(np.float32) - 128.0) / 128.0
    67	    else:
    68	        raise ValueError(f"未対応のwav sample width: {width}")
    69	    if channels > 1:
    70	        arr = arr.reshape(-1, channels)
    71	    return AudioBuffer(samples=arr, sample_rate=rate)
    72	
    73	
    74	class TTSClient:
    75	    """1つの httpx.AsyncClient を keep-alive で再利用する。"""
    76	
    77	    def __init__(self) -> None:
    78	        self._client = httpx.AsyncClient()
    79	
    80	    async def aclose(self) -> None:
    81	        await self._client.aclose()
    82	
    83	    def _build_request(self, profile: TTSProfile, body: dict[str, Any]) -> httpx.Request:
    84	        kwargs: dict[str, Any] = {"headers": profile.headers, "timeout": profile.timeout_sec}
    85	        if profile.method.upper() == "GET":
    86	            kwargs["params"] = body
    87	        elif profile.body_type == "json":
    88	            kwargs["json"] = body
    89	        elif profile.body_type in ("form", "multipart"):
    90	            kwargs["data"] = body
    91	        else:  # raw_template 等は将来対応
    92	            kwargs["json"] = body
    93	        return self._client.build_request(profile.method, profile.url, **kwargs)
    94	
    95	    def _decode(self, profile: TTSProfile, resp: httpx.Response) -> AudioBuffer:
    96	        data = resp.content
    97	        content_type = resp.headers.get("content-type", "").lower()
    98	        mode = profile.response_mode
    99	
   100	        if mode in ("audio_stream", "audio_file"):
   101	            if "wav" in content_type or data[:4] == b"RIFF":
   102	                return _decode_wav(data)
   103	            return _decode_raw(data, profile.audio)
   104	        if mode == "json_base64":
   105	            payload = resp.json()
   106	            field = _extract(payload, profile)
   107	            audio_bytes = base64.b64decode(field)
   108	            if audio_bytes[:4] == b"RIFF":
   109	                return _decode_wav(audio_bytes)
   110	            return _decode_raw(audio_bytes, profile.audio)
   111	        raise ValueError(f"未対応の response.mode: {mode}")
   112	
   113	    async def synthesize(self, profile: TTSProfile, text: str, **variables: Any) -> AudioBuffer:
   114	        body = profile.render_body(text, **variables)
   115	        request = self._build_request(profile, body)
   116	        resp = await self._client.send(request)
   117	        resp.raise_for_status()
   118	        return self._decode(profile, resp)
   119	
   120	
   121	def _extract(payload: dict, profile: TTSProfile) -> str:
   122	    """response.extract.path（ドット区切り）で JSON からフィールドを取り出す（json系用）。"""
   123	    # M1ではプロファイルにextract定義が無いので素朴に "audio" を探す。M6で schema 化。
   124	    if "audio" in payload:
   125	        return payload["audio"]
   126	    raise ValueError("json_base64 のフィールド抽出に失敗（extract.path 未対応）")

exec
/bin/bash -lc "nl -ba src/text_to_shaberi/audio/player.py | sed -n '1,180p'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
     1	"""音声再生（M1: ブロッキング再生＋出力rateへの soxr リサンプル）。
     2	
     3	出力デバイスは TTS の出力rate（例 32000Hz）を受け付けないことがある（生のALSA hwデバイス等は
     4	44100/48000固定）。そのため**出力デバイスの既定rateへ soxr で変換**してから再生する。
     5	チャンク逐次再生（OutputStream）と明示的なデバイス切替(drain/stop/reopen)は M2 で拡張する。
     6	"""
     7	from __future__ import annotations
     8	
     9	import numpy as np
    10	
    11	
    12	def _device_samplerate(sd, device: int | None) -> int:
    13	    info = sd.query_devices(kind="output") if device is None else sd.query_devices(device)
    14	    return int(info["default_samplerate"])
    15	
    16	
    17	def play(samples: np.ndarray, sample_rate: int, device: int | None = None) -> None:
    18	    """samples(float32 [-1,1]) を device で再生し、完了まで待つ（ブロッキング）。
    19	
    20	    出力デバイスの既定rateへ必要に応じてリサンプルする。別スレッド(executor)から呼ぶ前提。
    21	    """
    22	    import sounddevice as sd  # 遅延import（libportaudio2 が必要）
    23	
    24	    if device is None:
    25	        # PortAudioのdefaultは無音hwを指すことがあるため、PipeWire/pulse経由（=システム既定sink）を優先
    26	        from .devices import preferred_default_output
    27	
    28	        device = preferred_default_output()
    29	
    30	    target_sr = _device_samplerate(sd, device)
    31	    data = samples
    32	    if target_sr != sample_rate:
    33	        import soxr
    34	
    35	        data = soxr.resample(samples, sample_rate, target_sr)
    36	    sd.play(data, samplerate=target_sr, device=device)
    37	    sd.wait()
    38	
    39	
    40	def stop() -> None:
    41	    import sounddevice as sd
    42	
    43	    sd.stop()

exec
/bin/bash -lc "nl -ba src/text_to_shaberi/ui/main_window.py | sed -n '1,160p'" in /mnt/holyland/Project/Text-to-shaberi
 succeeded in 0ms:
     1	"""メインウィンドウ（M0: 足場）。
     2	
     3	テキスト入力・送信・出力デバイス選択・ログ表示の最小UI。
     4	実際のTTSパイプライン接続は M1 以降で `on_speak` に結線する。
     5	"""
     6	from __future__ import annotations
     7	
     8	from collections.abc import Callable
     9	
    10	from PySide6.QtWidgets import (
    11	    QComboBox,
    12	    QHBoxLayout,
    13	    QLabel,
    14	    QLineEdit,
    15	    QMainWindow,
    16	    QPlainTextEdit,
    17	    QPushButton,
    18	    QVBoxLayout,
    19	    QWidget,
    20	)
    21	
    22	
    23	class MainWindow(QMainWindow):
    24	    def __init__(self, config, output_devices=None, on_speak: Callable[[str], None] | None = None):
    25	        super().__init__()
    26	        self.config = config
    27	        self._on_speak = on_speak
    28	
    29	        self.setWindowTitle("Text-to-shaberi")
    30	        self.resize(680, 460)
    31	
    32	        central = QWidget()
    33	        layout = QVBoxLayout(central)
    34	
    35	        # 出力デバイス選択（既定 / 仮想オーディオ等）
    36	        device_row = QHBoxLayout()
    37	        device_row.addWidget(QLabel("出力先:"))
    38	        self.device_combo = QComboBox()
    39	        self.device_combo.addItem("既定デバイス（システム既定sink）", None)
    40	        for dev in output_devices or []:
    41	            self.device_combo.addItem(f"{dev.name}", dev.index)
    42	        device_row.addWidget(self.device_combo, 1)
    43	        layout.addLayout(device_row)
    44	
    45	        # テキスト入力 + 送信
    46	        input_row = QHBoxLayout()
    47	        self.text_input = QLineEdit()
    48	        self.text_input.setPlaceholderText("読み上げるテキストを入力…")
    49	        self.send_button = QPushButton("送信")
    50	        input_row.addWidget(self.text_input, 1)
    51	        input_row.addWidget(self.send_button)
    52	        layout.addLayout(input_row)
    53	
    54	        # ログ
    55	        self.log_view = QPlainTextEdit()
    56	        self.log_view.setReadOnly(True)
    57	        layout.addWidget(self.log_view, 1)
    58	
    59	        self.setCentralWidget(central)
    60	
    61	        self.send_button.clicked.connect(self._handle_send)
    62	        self.text_input.returnPressed.connect(self._handle_send)
    63	
    64	        self.log(f"profile = {config.active_tts_profile} / stt = {config.stt.backend}")
    65	
    66	    def set_on_speak(self, callback: Callable[[str], None]) -> None:
    67	        self._on_speak = callback
    68	
    69	    def selected_output_device(self) -> int | None:
    70	        return self.device_combo.currentData()
    71	
    72	    def _handle_send(self) -> None:
    73	        text = self.text_input.text().strip()
    74	        if not text:
    75	            return
    76	        self.text_input.clear()
    77	        self.log(f"[gui] {text}")
    78	        if self._on_speak is not None:
    79	            self._on_speak(text)
    80	
    81	    def log(self, message: str) -> None:
    82	        self.log_view.appendPlainText(message)

codex
**レビュー結果**

計画の方向性は妥当です。既存の M1 実装は `TTSClient.synthesize()` が全受信、`audio.player.play()` が一括リサンプル再生なので、M2 で `httpx` streaming + `sd.OutputStream` + `soxr.ResampleStream` に移す方針は既存コードと整合します。

ただし、このまま GLM-5.1 に実装委託すると、HTTP ストリーム構築、停止時キャンセル、PCM 変換、仮想 sink ルーティングで迷う、またはバグる可能性が高い箇所があります。

**指摘**

1. **高: `httpx.AsyncClient.stream()` への `Request` 流用案が不完全**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:27), [client.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/tts/client.py:83)

   計画のサンプルは `_build_request()` が返す `httpx.Request` から `method/url/content/headers` を取り出して `self._client.stream(...)` に渡す案ですが、GET の `params` は `request.url` に含まれる一方、`content=request.content` を常に渡す設計はやや危ういです。また `build_request()` 側の `timeout` は `Request` に保持されないため、stream 側で再指定しているのは正しいですが、今後 `data/json/params/files` が増えると破綻しやすいです。

   修正提案:
   - `_build_request_kwargs(profile, body) -> tuple[method, url, kwargs]` を新設し、`synthesize()` と `stream()` の両方で使う計画に明記する。
   - `stream()` は `async with self._client.stream(method, url, **kwargs)` とする。
   - `multipart` は現状 `data` 扱いなので、M2では未対応または簡易対応であることを明記する。

2. **高: 停止ボタンが HTTP ストリームをキャンセルしない**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:89), [app.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:123)

   計画では `current_player[0].abort()` のみです。これだと音は止まっても `async for chunk in client.stream(...)` は継続し、以後の `player.write_bytes()` が閉じた stream に書き込んで例外になる可能性があります。さらに TTS サーバ側の生成もキャンセルされず、SLO の「割り込み→無音化」と M3 のキャンセル設計に対して中途半端です。

   修正提案:
   - `current_task: asyncio.Task | None` も保持し、停止時は `player.abort()` に加えて `current_task.cancel()` する。
   - `speak()` は `asyncio.CancelledError` を明示的に捕捉し、`player.abort()` とログを出して再 raise しない、または正常停止扱いにする。
   - `client.stream()` の `async with` をキャンセルで確実に抜けることを受け入れ基準に入れる。
   - `aboutToQuit` 時も `current_task.cancel()` と player abort を行う計画にする。

3. **高: `OutputStream.stop()` では末尾を drain できない可能性がある**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:52)

   計画では `close()` で resampler tail を write したあと `self._stream.stop(); close()` です。sounddevice/PortAudio の `stop()` は pending buffer を最後まで鳴らす保証ではなく、環境によっては末尾が切れる可能性があります。通常完了と abort を明確に分けるべきです。

   修正提案:
   - 通常完了は `write(tail)` のあと `self._stream.close()` か、必要なら `stop(ignore_errors=True)` の挙動を確認して明記する。
   - 即停止だけ `abort()` を使う。
   - 末尾欠けを検出するテスト、少なくとも短い sine を最後まで鳴らす手動確認を追加する。

4. **高: PCM 24bit の扱いが既存実装と計画で危険**
   
   該当: [client.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/tts/client.py:21), [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:36)

   既存 `_PCM_DTYPE` は `pcm_s24le` を `<i4>` として近似していますが、raw 24bit PCM は 3 bytes/sample です。ストリーミングでは境界 carry の `bytes_per_sample()` と dtype 変換が絡むため、ここを曖昧にするとサンプル境界が壊れます。現プロファイルは `pcm_s16le` なので実害は避けられますが、共通関数化時に地雷になります。

   修正提案:
   - M2 の対応 encoding を `pcm_s16le`, `pcm_s32le`, `pcm_f32le` に限定し、`pcm_s24le` は `NotImplementedError` にする。
   - どうしても 24bit 対応するなら、3 byte little-endian を手動 sign-extend する実装仕様を計画に書く。
   - `_pcm_to_float32()` は不明 encoding を黙って `<i2>` にフォールバックしない。

5. **中: `_pcm_to_float32()` の shape 仕様が既存 `AudioBuffer` コメントと微妙にズレる**
   
   該当: [client.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/tts/client.py:33), [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:49)

   `AudioBuffer.samples` はコメント上 `(frames, channels)` ですが、既存 `_decode_raw()` は mono だと 1D を返します。計画も mono は 1D としています。sounddevice と soxr 的には動きますが、契約が曖昧です。

   修正提案:
   - `AudioBuffer.samples` のコメントを「mono は 1D、多ch は 2D」に直す計画を追加する。
   - `_pcm_to_float32(data, encoding, channels)` の戻り shape を明記する。
   - `channels > 1` で端数 frames が出たら reshape 前に明示エラーまたは carry で処理する。

6. **中: TTFA 計測が first-chunk であり、実際の first-audio ではない**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:67)

   計画は `first = loop.time()` を最初の HTTP chunk 到達時に記録しています。これは first-byte/first-chunk であり、OutputStream に書かれて実際に音が出る時刻ではありません。OutputStream 先行 open は良いですが、`run_in_executor(player.write_bytes)` と PortAudio バッファ分の遅れが含まれません。

   修正提案:
   - ログ名を `first-chunk` と `first-write` に分ける。
   - SLO の TTFA 判定は M2 では近似として `first_write_ms + stream.latency/output buffer` と明記する。
   - `OutputStream` の `blocksize` / `latency` を計画に入れる。低遅延狙いなら `latency="low"` または小さめ blocksize の検証が必要。

7. **中: underrun 対策が「逐次 write」だけで弱い**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:47)

   HTTP chunk が細かすぎる、または TTS 側の生成間隔が揺れると、`OutputStream.write()` が間に合わず underrun が起きます。先行 open は初動には効きますが、ジッタ吸収用の最小プリロールがありません。

   修正提案:
   - 最初の write 前に `N ms` 分、例えば 20-60ms の PCM/resampled frames を溜めるか、まず直書きで始める場合は underrun リスクを受け入れ基準に明記する。
   - `sd.OutputStream(..., latency="low")` とプリロール量を実測で調整する項目を追加する。
   - `OutputStream.write()` 例外時に abort/close する処理を明記する。

8. **高: 仮想 sink の sink-input 特定が現実には競合しやすい**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:93)

   `application.process.id` または `application.name` で特定する方針は best-effort としては妥当ですが、同一プロセスで複数 stream、過去の sink-input、他の PortAudio stream があると誤爆します。また `PULSE_PROP` を Python プロセス内で `os.environ` 設定しても、PortAudio がどの時点で Pulse/PipeWire client properties を読むかは実装依存です。

   修正提案:
   - `pactl list sink-inputs` は `application.process.id == os.getpid()` かつ `application.name == text-to-shaberi` かつ state/created time が新しいもの、という選択規則を明記する。
   - `move_app_to_sink()` は「移動した sink-input id」を返し、見つからない/複数候補は警告して移動しない。
   - `pactl` 不在、`ttsvirt` sink 不在、move 失敗を個別ログにする。
   - `TTS_Virtual` 選択時の `selected_output_device()` は `int | None` ではなく `int | None | str` になるため、型変更と app 側分岐を明記する。

9. **中: `audio/routing.py` が任意になっているが、実装委託には必須化した方がよい**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:102)

   仮想 sink を M2 受け入れ基準に入れるなら、routing 分離を任意にすると GLM が `app.py` に雑に subprocess 処理を埋める可能性があります。

   修正提案:
   - `audio/routing.py` を必須変更ファイルにする。
   - API を `move_app_to_sink(sink_name: str, app_name: str, pid: int) -> bool | int` のように固定する。
   - subprocess は timeout 付き、`capture_output=True`, `text=True`, shell 不使用を明記する。

10. **中: 同時発話の扱いが未定義**
    
    該当: [app.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:134)

    現状は送信ボタンを連打すると `speak()` task が複数走ります。M2 はキュー非スコープですが、StreamingPlayer を複数開くと音が重なり、`current_player[0]` も最後のものだけを指します。

    修正提案:
    - M2 では「再生中の送信は拒否してログ」または「現在の発話を abort/cancel して新規開始」のどちらかを明記する。
    - 最小実装なら `current_task` が未完了なら送信ボタンを disable、完了後 enable。
    - これは M3 キューまでの暫定仕様として計画に入れるべきです。

11. **低: `PULSE_PROP` 設定位置が曖昧**
    
    該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:100), [app.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:103)

    「起動時 os.environ に」とありますが、`QApplication` 前なのか、sounddevice import 前なのかが重要です。

    修正提案:
    - `run_gui()` の `QApplication` 生成前、かつ sounddevice import/デバイス列挙前に設定する、と明記する。
    - ただし `_load_output_devices()` は window 作成時に sounddevice を import するため、その前に置く必要があります。

12. **低: テスト計画が自動検証として弱い**
    
    該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:119)

    `StreamingPlayer` の実音テストは環境依存なので仕方ないですが、PCM carry と resampler flush はユニットテストしやすい箇所です。

    修正提案:
    - `pcm_s16le` の奇数 byte chunk 分割で carry が次回に回るテストを追加する。
    - `write_bytes(b"\x00")` のあと `write_bytes(b"\x00")` で 1 sample として処理されることを検証する。
    - sounddevice を mock/fake stream に差し替えられるよう `StreamingPlayer` 内部を少し分離するか、最低限 `_pcm_to_float32()` 単体テストを追加する。

**既存コードとの整合評価**

- `profile.audio.sample_rate/channels/encoding` は既にあり、StreamingPlayer の入力に使えます。
- `_device_samplerate(sd, device)` と `preferred_default_output()` は計画通り利用可能です。
- `MainWindow.selected_output_device()` は現在 `int | None` 前提なので、`"__ttsvirt__"` センチネル導入時に型と app 分岐の変更が必要です。
- `_decode_raw()` の共通化は妥当ですが、既存の暗黙 fallback と 24bit 近似は M2 で整理すべきです。
- `app.aboutToQuit` の `client.aclose()` 維持だけでは不十分で、進行中 task/player の停止が必要です。

**総評**

計画は M2 の実装方針として成立しています。特に `httpx.aiter_bytes()`、PCM carry、`soxr.ResampleStream.resample_chunk(..., last=True)`、`run_in_executor` 逐次 await による write 順序保証という中核設計は正しいです。

一方で、実装委託用の仕様としてはまだ「実装者の判断に任せすぎる」箇所があります。特に停止時の HTTP キャンセル、`_build_request` の stream 共有化、仮想 sink の候補選択規則は、曖昧なままだと動くが不安定な実装になりやすいです。

**実装委託前に計画へ必ず反映すべき TOP3**

1. `current_task` を導入し、停止ボタンで `player.abort()` と TTS HTTP stream の task cancel を両方行う仕様にする。
2. `_build_request` を request kwargs 生成関数へリファクタし、`synthesize()` と `stream()` が同じ GET/json/form ロジックを使う設計に固定する。
3. `TTS_Virtual` の `"__ttsvirt__"` 分岐、`audio/routing.py` 必須化、sink-input 候補選択/失敗時ログ/timeout 付き subprocess 仕様を明文化する。
hook: Stop
hook: Stop Completed
tokens used
39,187
**レビュー結果**

計画の方向性は妥当です。既存の M1 実装は `TTSClient.synthesize()` が全受信、`audio.player.play()` が一括リサンプル再生なので、M2 で `httpx` streaming + `sd.OutputStream` + `soxr.ResampleStream` に移す方針は既存コードと整合します。

ただし、このまま GLM-5.1 に実装委託すると、HTTP ストリーム構築、停止時キャンセル、PCM 変換、仮想 sink ルーティングで迷う、またはバグる可能性が高い箇所があります。

**指摘**

1. **高: `httpx.AsyncClient.stream()` への `Request` 流用案が不完全**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:27), [client.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/tts/client.py:83)

   計画のサンプルは `_build_request()` が返す `httpx.Request` から `method/url/content/headers` を取り出して `self._client.stream(...)` に渡す案ですが、GET の `params` は `request.url` に含まれる一方、`content=request.content` を常に渡す設計はやや危ういです。また `build_request()` 側の `timeout` は `Request` に保持されないため、stream 側で再指定しているのは正しいですが、今後 `data/json/params/files` が増えると破綻しやすいです。

   修正提案:
   - `_build_request_kwargs(profile, body) -> tuple[method, url, kwargs]` を新設し、`synthesize()` と `stream()` の両方で使う計画に明記する。
   - `stream()` は `async with self._client.stream(method, url, **kwargs)` とする。
   - `multipart` は現状 `data` 扱いなので、M2では未対応または簡易対応であることを明記する。

2. **高: 停止ボタンが HTTP ストリームをキャンセルしない**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:89), [app.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:123)

   計画では `current_player[0].abort()` のみです。これだと音は止まっても `async for chunk in client.stream(...)` は継続し、以後の `player.write_bytes()` が閉じた stream に書き込んで例外になる可能性があります。さらに TTS サーバ側の生成もキャンセルされず、SLO の「割り込み→無音化」と M3 のキャンセル設計に対して中途半端です。

   修正提案:
   - `current_task: asyncio.Task | None` も保持し、停止時は `player.abort()` に加えて `current_task.cancel()` する。
   - `speak()` は `asyncio.CancelledError` を明示的に捕捉し、`player.abort()` とログを出して再 raise しない、または正常停止扱いにする。
   - `client.stream()` の `async with` をキャンセルで確実に抜けることを受け入れ基準に入れる。
   - `aboutToQuit` 時も `current_task.cancel()` と player abort を行う計画にする。

3. **高: `OutputStream.stop()` では末尾を drain できない可能性がある**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:52)

   計画では `close()` で resampler tail を write したあと `self._stream.stop(); close()` です。sounddevice/PortAudio の `stop()` は pending buffer を最後まで鳴らす保証ではなく、環境によっては末尾が切れる可能性があります。通常完了と abort を明確に分けるべきです。

   修正提案:
   - 通常完了は `write(tail)` のあと `self._stream.close()` か、必要なら `stop(ignore_errors=True)` の挙動を確認して明記する。
   - 即停止だけ `abort()` を使う。
   - 末尾欠けを検出するテスト、少なくとも短い sine を最後まで鳴らす手動確認を追加する。

4. **高: PCM 24bit の扱いが既存実装と計画で危険**
   
   該当: [client.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/tts/client.py:21), [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:36)

   既存 `_PCM_DTYPE` は `pcm_s24le` を `<i4>` として近似していますが、raw 24bit PCM は 3 bytes/sample です。ストリーミングでは境界 carry の `bytes_per_sample()` と dtype 変換が絡むため、ここを曖昧にするとサンプル境界が壊れます。現プロファイルは `pcm_s16le` なので実害は避けられますが、共通関数化時に地雷になります。

   修正提案:
   - M2 の対応 encoding を `pcm_s16le`, `pcm_s32le`, `pcm_f32le` に限定し、`pcm_s24le` は `NotImplementedError` にする。
   - どうしても 24bit 対応するなら、3 byte little-endian を手動 sign-extend する実装仕様を計画に書く。
   - `_pcm_to_float32()` は不明 encoding を黙って `<i2>` にフォールバックしない。

5. **中: `_pcm_to_float32()` の shape 仕様が既存 `AudioBuffer` コメントと微妙にズレる**
   
   該当: [client.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/tts/client.py:33), [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:49)

   `AudioBuffer.samples` はコメント上 `(frames, channels)` ですが、既存 `_decode_raw()` は mono だと 1D を返します。計画も mono は 1D としています。sounddevice と soxr 的には動きますが、契約が曖昧です。

   修正提案:
   - `AudioBuffer.samples` のコメントを「mono は 1D、多ch は 2D」に直す計画を追加する。
   - `_pcm_to_float32(data, encoding, channels)` の戻り shape を明記する。
   - `channels > 1` で端数 frames が出たら reshape 前に明示エラーまたは carry で処理する。

6. **中: TTFA 計測が first-chunk であり、実際の first-audio ではない**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:67)

   計画は `first = loop.time()` を最初の HTTP chunk 到達時に記録しています。これは first-byte/first-chunk であり、OutputStream に書かれて実際に音が出る時刻ではありません。OutputStream 先行 open は良いですが、`run_in_executor(player.write_bytes)` と PortAudio バッファ分の遅れが含まれません。

   修正提案:
   - ログ名を `first-chunk` と `first-write` に分ける。
   - SLO の TTFA 判定は M2 では近似として `first_write_ms + stream.latency/output buffer` と明記する。
   - `OutputStream` の `blocksize` / `latency` を計画に入れる。低遅延狙いなら `latency="low"` または小さめ blocksize の検証が必要。

7. **中: underrun 対策が「逐次 write」だけで弱い**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:47)

   HTTP chunk が細かすぎる、または TTS 側の生成間隔が揺れると、`OutputStream.write()` が間に合わず underrun が起きます。先行 open は初動には効きますが、ジッタ吸収用の最小プリロールがありません。

   修正提案:
   - 最初の write 前に `N ms` 分、例えば 20-60ms の PCM/resampled frames を溜めるか、まず直書きで始める場合は underrun リスクを受け入れ基準に明記する。
   - `sd.OutputStream(..., latency="low")` とプリロール量を実測で調整する項目を追加する。
   - `OutputStream.write()` 例外時に abort/close する処理を明記する。

8. **高: 仮想 sink の sink-input 特定が現実には競合しやすい**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:93)

   `application.process.id` または `application.name` で特定する方針は best-effort としては妥当ですが、同一プロセスで複数 stream、過去の sink-input、他の PortAudio stream があると誤爆します。また `PULSE_PROP` を Python プロセス内で `os.environ` 設定しても、PortAudio がどの時点で Pulse/PipeWire client properties を読むかは実装依存です。

   修正提案:
   - `pactl list sink-inputs` は `application.process.id == os.getpid()` かつ `application.name == text-to-shaberi` かつ state/created time が新しいもの、という選択規則を明記する。
   - `move_app_to_sink()` は「移動した sink-input id」を返し、見つからない/複数候補は警告して移動しない。
   - `pactl` 不在、`ttsvirt` sink 不在、move 失敗を個別ログにする。
   - `TTS_Virtual` 選択時の `selected_output_device()` は `int | None` ではなく `int | None | str` になるため、型変更と app 側分岐を明記する。

9. **中: `audio/routing.py` が任意になっているが、実装委託には必須化した方がよい**
   
   該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:102)

   仮想 sink を M2 受け入れ基準に入れるなら、routing 分離を任意にすると GLM が `app.py` に雑に subprocess 処理を埋める可能性があります。

   修正提案:
   - `audio/routing.py` を必須変更ファイルにする。
   - API を `move_app_to_sink(sink_name: str, app_name: str, pid: int) -> bool | int` のように固定する。
   - subprocess は timeout 付き、`capture_output=True`, `text=True`, shell 不使用を明記する。

10. **中: 同時発話の扱いが未定義**
    
    該当: [app.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:134)

    現状は送信ボタンを連打すると `speak()` task が複数走ります。M2 はキュー非スコープですが、StreamingPlayer を複数開くと音が重なり、`current_player[0]` も最後のものだけを指します。

    修正提案:
    - M2 では「再生中の送信は拒否してログ」または「現在の発話を abort/cancel して新規開始」のどちらかを明記する。
    - 最小実装なら `current_task` が未完了なら送信ボタンを disable、完了後 enable。
    - これは M3 キューまでの暫定仕様として計画に入れるべきです。

11. **低: `PULSE_PROP` 設定位置が曖昧**
    
    該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:100), [app.py](/mnt/holyland/Project/Text-to-shaberi/src/text_to_shaberi/app.py:103)

    「起動時 os.environ に」とありますが、`QApplication` 前なのか、sounddevice import 前なのかが重要です。

    修正提案:
    - `run_gui()` の `QApplication` 生成前、かつ sounddevice import/デバイス列挙前に設定する、と明記する。
    - ただし `_load_output_devices()` は window 作成時に sounddevice を import するため、その前に置く必要があります。

12. **低: テスト計画が自動検証として弱い**
    
    該当: [docs/IMPL-M2.md](/mnt/holyland/Project/Text-to-shaberi/docs/IMPL-M2.md:119)

    `StreamingPlayer` の実音テストは環境依存なので仕方ないですが、PCM carry と resampler flush はユニットテストしやすい箇所です。

    修正提案:
    - `pcm_s16le` の奇数 byte chunk 分割で carry が次回に回るテストを追加する。
    - `write_bytes(b"\x00")` のあと `write_bytes(b"\x00")` で 1 sample として処理されることを検証する。
    - sounddevice を mock/fake stream に差し替えられるよう `StreamingPlayer` 内部を少し分離するか、最低限 `_pcm_to_float32()` 単体テストを追加する。

**既存コードとの整合評価**

- `profile.audio.sample_rate/channels/encoding` は既にあり、StreamingPlayer の入力に使えます。
- `_device_samplerate(sd, device)` と `preferred_default_output()` は計画通り利用可能です。
- `MainWindow.selected_output_device()` は現在 `int | None` 前提なので、`"__ttsvirt__"` センチネル導入時に型と app 分岐の変更が必要です。
- `_decode_raw()` の共通化は妥当ですが、既存の暗黙 fallback と 24bit 近似は M2 で整理すべきです。
- `app.aboutToQuit` の `client.aclose()` 維持だけでは不十分で、進行中 task/player の停止が必要です。

**総評**

計画は M2 の実装方針として成立しています。特に `httpx.aiter_bytes()`、PCM carry、`soxr.ResampleStream.resample_chunk(..., last=True)`、`run_in_executor` 逐次 await による write 順序保証という中核設計は正しいです。

一方で、実装委託用の仕様としてはまだ「実装者の判断に任せすぎる」箇所があります。特に停止時の HTTP キャンセル、`_build_request` の stream 共有化、仮想 sink の候補選択規則は、曖昧なままだと動くが不安定な実装になりやすいです。

**実装委託前に計画へ必ず反映すべき TOP3**

1. `current_task` を導入し、停止ボタンで `player.abort()` と TTS HTTP stream の task cancel を両方行う仕様にする。
2. `_build_request` を request kwargs 生成関数へリファクタし、`synthesize()` と `stream()` が同じ GET/json/form ロジックを使う設計に固定する。
3. `TTS_Virtual` の `"__ttsvirt__"` 分岐、`audio/routing.py` 必須化、sink-input 候補選択/失敗時ログ/timeout 付き subprocess 仕様を明文化する。
