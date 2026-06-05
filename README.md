# Text-to-shaberi

**マイク音声／テキストを、任意の TTS-API サーバ経由で「しゃべらせる」低遅延デスクトップアプリ。**

マイクに話す → 無音検出で発話を区切る → サーバで STT（音声認識）→ 認識文を自動で TTS 合成 → クライアントで再生。テキスト入力での直接読み上げにも対応。AITuber／配信・ボイスチェンジ・読み上げ／アクセシビリティ用途を想定。

GPU を載せた Linux サーバ（STT＋TTS）と、手元の Windows／Linux クライアント（マイク・GUI・再生）を **WebSocket で分離**した構成です。

---

## 特長

- 🎤 **常時マイク＋VAD**: webrtcvad による無音区切り＋軽量ノイズゲート。発話単位で自動送信。
- 🗣 **STT → 自動 TTS 連鎖**: Qwen3-ASR で認識 → そのまま GPT-SoVITS で合成・再生（`auto_tts`）。
- ⏩ **ストリーミングモード（喋りながら順次TTS）**: 句の切れ目で先行 STT → 順次合成 → **1本の連続音声**で返す。発話単位モードとはメイン画面のトグルで切替（サーバ対応時）。→ [`docs/STREAMING.md`](docs/STREAMING.md)
- 🤖 **ローカル入力口（AITuber/LLM連携）**: `[input]` を有効化すると `ws://127.0.0.1:<port>` でテキストを受信し、文単位で先行合成 → 連続音声で配信へ。用途別の使い方は [`docs/USECASES.md`](docs/USECASES.md)。
- ⌨ **テキスト読み上げ**: GUI のテキスト入力／LLM 等からの送信を想定。
- 🔌 **任意の TTS-API に対応**: リクエストボディを **TOML プロファイル**で定義（TTS ごとに差し替え）。
- 🎧 **低遅延ストリーミング再生**: 先頭チャンクから逐次再生（先頭欠けなし）。出力先は既定デバイス／仮想シンク（配信用）を選択。
- 📦 **Opus 圧縮**: TTS 音声を Opus で送信し帯域を約 1/30 に（実測 ≈35x／非対応時は自動 PCM フォールバック）。
- 🔐 **セキュリティ**: `Authorization: Bearer` トークン認証＋ `wss://`（TLS）対応。
- 🔁 **自動再接続**: サーバ再起動・回線断から指数バックオフで自動復帰。
- ⚙ **GUI 設定**: サーバ／マイク／出力を歯車ダイアログで編集（`client.toml` に保存）。STT 結果・TTFA・コーデックをステータス表示。
- 🇯🇵 **日本語入力**: ibus+mozc／Qt 仮想キーボードを選択可。

---

## アーキテクチャ

```
┌──────────── クライアント (Windows / Linux) ───────────┐        ┌──────── サーバ (Linux + GPU) ────────┐
│ PySide6 GUI ＋ qasync (Qt/asyncio 同一ループ)          │        │ FastAPI + uvicorn (WebSocket /ws)     │
│                                                       │        │                                       │
│  マイク ─ webrtcvad ─┐                                 │  WS    │  ┌ STT: Qwen3-ASR (GPU常駐)           │
│  (常時録音/無音区切り) │→ stt_begin/binary/stt_end ────┼───────►│  │   発話単位で文字起こし → transcript │
│                       │                                │ (opus/ │  └ TTS: GPT-SoVITS api_v2 (HTTP :9880)│
│  テキスト ───────────┘→ speak ─────────────────────────┤  pcm + │      認識文/テキストを合成          │
│                                                       │ token/ │      → audio_begin/binary/audio_end  │
│  再生 ◄─ Opus/PCM デコード ◄─ StreamingPlayer ◄────────┤  wss)  │  (auto_tts: transcript→TTS を自動連鎖)│
│       → 既定デバイス / 仮想シンク(配信)                 │        │  GPU直列化 (single-worker executor)  │
└───────────────────────────────────────────────────────┘        └───────────────────────────────────────┘
```

- **STT（Qwen3-ASR）と TTS（GPT-SoVITS）はサーバ側 GPU**。GPT-SoVITS は独立プロセス（HTTP :9880）として動かし、サーバから叩きます。
- クライアントは **マイク取得・GUI・音声再生**のみ。重い処理は持ちません（Windows でも軽量）。
- サーバに直接つないで**合成音声ストリームを受信する外部ソフト向けの接続仕様**は [`docs/PROTOCOL.md`](docs/PROTOCOL.md)（WebSocket メッセージ／Opus・PCM フレーム形式／受信サンプル）。
- 詳細な設計・各マイルストーン記録は `docs/`（`ARCH-v0.3-client-server.md` / `IMPL-*.md` / `CODE-REVIEW-*.md`）。

---

## 必要環境

| | サーバ | クライアント |
|---|---|---|
| OS | Linux（CUDA GPU） | Windows / Linux |
| Python | 3.12+ | 3.12+ |
| パッケージ管理 | [uv](https://docs.astral.sh/uv/) | uv |
| GPU | NVIDIA（STT/TTS 常駐・実測 VRAM 約 5〜12GB） | 不要 |
| 外部依存 | **GPT-SoVITS api_v2**（別プロセス）／Qwen3-ASR（自動DL）／（任意）仮想オーディオ | PortAudio（Linux）／libopus DLL（Windows・Opus 使用時） |

> GPT-SoVITS・Qwen3-ASR・仮想オーディオ（PipeWire null sink）の**詳細な構築手順**は **[`docs/SETUP.md`](docs/SETUP.md)** にあります（このマシン向けに具体化済み）。本 README は導入の要点をまとめます。

---

## 導入（インストール）

リポジトリは 1 つの uv パッケージで、`server` / `client` の **optional-extras** で役割を分けます。サーバ機ではサーバ extra を、クライアント機ではクライアント extra を入れます。

```bash
git clone <this-repo> text-to-shaberi
cd text-to-shaberi
```

### A. サーバ（Linux + GPU）

1. **GPT-SoVITS api_v2 を別プロセスで用意**（conda 推奨）。取得・参照音声・起動・疎通確認は **[`docs/SETUP.md`](docs/SETUP.md) §3** を参照。要点だけ:
   ```bash
   conda activate GPTSoVits
   cd /path/to/GPT-SoVITS
   python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
   ```
2. **サーバ依存をインストール**（PyTorch は `pyproject.toml` の `[[tool.uv.index]]`＝CUDA cu128 ホイールを使用）:
   ```bash
   uv sync --extra server
   ```
   - Qwen3-ASR モデル（`Qwen/Qwen3-ASR-1.7B`）は初回起動時に自動 DL。事前 DL は `uv run huggingface-cli download Qwen/Qwen3-ASR-1.7B`。
3. **設定** `server.toml` を編集（下記「設定」参照）。
4. **起動**:
   ```bash
   uv run text-to-shaberi-server          # 既定: 127.0.0.1:8765/ws
   uv run text-to-shaberi-server -c /path/to/server.toml   # 設定ファイル指定
   ```
   - 起動ログに `[opus] available: True` と `Uvicorn running on http://127.0.0.1:8765` が出れば OK。

### A-2. サーバ（Docker compose — GPT-SoVITS 同梱）

A のネイティブ手順の代わりに、**サーバ側一式（orchestrator + GPT-SoVITS api_v2）を `docker compose` で立ち上げる**こともできます（クライアントは対象外＝ネイティブのまま）。設計詳細は [`docs/IMPL-DOCKER.md`](docs/IMPL-DOCKER.md)。

前提: Linux + NVIDIA GPU、`docker` / `docker compose`（v2+）/ **nvidia-container-toolkit**（`deploy.resources.reservations.devices` で GPU を割り当て）。GPT-SoVITS は重み入りリポジトリをそのまま bind-mount するため、ローカルに `GPT-SoVITS`（`GPT_SoVITS/pretrained_models/*` と `text/G2PWModel` を含む）が必要。

> ⚠️ **compose は native 版の「置き換え」**。同じ `8765`/`9880` とGPUを使うため、**native の orchestrator / GPT-SoVITS を起動したまま `up` するとポート衝突します**。先に native を停止してください。

```bash
# 1. 設定を作る
cp deploy/server.docker.toml.example deploy/server.docker.toml   # token を設定（例: openssl rand -hex 24）
cp deploy/.env.example .env                                      # GPTSOVITS_DIR / SHABERI_ASSETS / GPU_ID / WS_PORT を環境に合わせる

# 2. ビルド & 起動（初回は torch cu128 取得でイメージ ~25GB / Qwen 自動DL / SoVITS ロードで数分）
docker compose build
docker compose up -d

# 3. ロード完了をログで確認
docker compose logs -f gpt-sovits      # api_v2 が :9880 で listen
docker compose logs -f orchestrator    # [opus] available: True / Uvicorn running

# 4. クライアント（Windows）の client.toml:
#    [server].address = ws://<linux-host-ip>:8765/ws, token = <同じ値>
```

- `docker-compose.yml`（repo直下）= `gpt-sovits` + `orchestrator` の2サービス。`.env` でパス/GPU/ポートを上書き。
- `server.docker.toml` は **token 入りのため gitignore**。compose が read-only でマウント（`profiles/` も同様にマウントし、再ビルドなしで編集可）。
- Qwen 重みは名前付きボリューム `hf-cache` に永続化（再起動で再DLしない）。
- GPU を複数枚から選ぶ場合は `.env` の `GPU_ID`。24GB 1枚に Qwen + GPT-SoVITS は同居可能。

> **重要 — `GPTSOVITS_DIR` はマシン毎に必須設定**。プレビルドイメージは GPT-SoVITS の*コードのみ*を持ち、**重みは持ちません**。重みは `.env` の `GPTSOVITS_DIR`（`api_v2.py` と `GPT_SoVITS/pretrained_models`・`text/G2PWModel` を含む実在ローカルrepo）を bind-mount して供給します。
> - 存在しないパスを指すと Docker が空ディレクトリをマウントしてイメージ内 `api_v2.py` が隠れ、`gpt-sovits` が `python: can't open file '/workspace/GPT-SoVITS/api_v2.py'` で再起動ループ → orchestrator も `dependency failed to start`。
> - 起動前チェック: `ls "$GPTSOVITS_DIR/api_v2.py"` と `ls "$SHABERI_ASSETS/ref.wav"`（`ref.wav` は gitignore 済みなので新規 clone には無い）。
> - compose 側にも fail-fast を入れてあり、未配置時は `gpt-sovits` ログに `FATAL: ... api_v2.py がありません ...` を表示します。

### B. クライアント（Windows / Linux）

1. **クライアント依存をインストール**（torch/qwen を含まない軽量セット）:
   ```bash
   uv sync --extra client
   ```
   - **Linux**: PortAudio が必要 → `sudo apt install -y libportaudio2`（音声 I/O）。日本語入力に ibus+mozc を使う場合は `ibus`/`mozc` を導入。
   - **Windows（Opus 使用時）**: `opuslib` が `opus.dll` / `libopus-0.dll` を必要とします。配置手順は [`docs/SETUP.md`](docs/SETUP.md) §9.2。未配置でも自動的に PCM にフォールバックします。
2. **設定** `client.toml` の `[server].address`（と外部接続時の `token`）を編集。
3. **設定／デバイスの事前チェック**（GUI を起動せず確認）:
   ```bash
   uv run text-to-shaberi-client --check
   ```
   - `server.address` / `codec` / `opus available` / 出力デバイス一覧 / mic 設定が表示されます。
4. **起動**:
   ```bash
   uv run text-to-shaberi-client          # GUI 起動（= text-to-shaberi でも可）
   ```

### 使い方（GUI）

- **テキスト読み上げ**: 入力欄に文字を入れて「送信」。「停止」で即中断。
- **マイク**: 「🎤」トグルを ON にすると常時録音開始。話して黙ると自動で区切られ、`🎤→ 認識文` がログに出て、TTS 音声が再生されます（再生中はマイクが一時停止しフィードバックを防止）。
- **モード切替**: メイン画面のトグルで「発話単位」⇄「ストリーミング（喋りながら順次TTS）」を切替（サーバが対応している時のみ有効）。詳細・チューニングは [`docs/STREAMING.md`](docs/STREAMING.md)。
- **出力先**: 「既定デバイス」／「TTS_Virtual（配信用）」／列挙された各デバイスを選択。
- **⚙ 設定**: サーバ（アドレス／トークン／コーデック／TLS）・マイク（デバイス／VAD／ゲート）・出力デバイスを編集し `client.toml` に保存（サーバ系変更は自動再接続）。
- 下部ステータス行に `接続 ✓ codec:opus | STT 1.20s | TTFA 130ms` を表示。

---

## 設定

### `server.toml`

| セクション | キー | 説明 |
|---|---|---|
| `[bind]` | `host` / `port` | 待受。`host` を loopback 以外にするなら `token` 必須。 |
| | `token` | Bearer トークン（空＝認証なし＝loopback ローカル運用のみ）。 |
| | `tls_cert` / `tls_key` | 両方指定で uvicorn ネイティブ TLS（`wss://`）。 |
| `[stt]` | `backend` / `language` | `qwen_local`（既定）。`language` は `ja`/`en`/`zh`。 |
| `[stt.qwen_local]` | `model` / `device` / `dtype` / `attn_implementation` | Qwen3-ASR の設定。flash-attn 導入済みなら `flash_attention_2`。 |
| `[tts]` | `profiles_dir` / `active_profile` | TTS プロファイル（既定 `profiles/tts/<name>.toml` の `gpt-sovits`）。 |
| `[transport]` | `codec` / `opus_bitrate` | `opus`（既定・非対応クライアントへは自動 PCM）/`pcm`。 |

### `client.toml`

| セクション | 主なキー | 説明 |
|---|---|---|
| `[server]` | `address` / `token` | 接続先 `ws://`／`wss://` と Bearer トークン。 |
| | `codec` | `opus`（既定・opuslib 不在なら自動 pcm）/`pcm`。 |
| | `tls_insecure` / `tls_cafile` | 自己署名証明書の検証無効（LAN 用）／カスタム CA パス。 |
| | `reconnect` / `reconnect_max_delay` | 自動再接続（既定 on）とバックオフ上限秒。 |
| `[output]` | `device` | `default` / デバイス名の一部 / index。 |
| `[mic]` | `device` / `sample_rate` / `channels` / `stream_mode` | 取込（既定 16000Hz mono）。`stream_mode=true` で既定ストリーミング。 |
| `[mic.preprocess]` | `gate` / `gate_threshold` | ノイズゲート（判定の床）。 |
| `[mic.vad]` | `aggressiveness` / `min_silence_ms` / `speech_pad_ms` / `frame_ms` / `min_speech_ms` / `max_speech_ms` | webrtcvad の発話区切りパラメタ（発話単位モード）。`frame_ms`∈{10,20,30}・`sample_rate`∈{8/16/32/48k}。 |
| | `seg_silence_ms` / `end_silence_ms` / `seg_min_ms` / `seg_max_ms` | ストリーミングモードの句区切り／終端／seg 長。意味・チューニングは [`docs/STREAMING.md`](docs/STREAMING.md)。 |
| `[ui]` | `input_method` | `ibus`（既定）/`virtualkeyboard`/`system`。 |
| `[profile]` | `active_id` | 使用する TTS プロファイル ID（空＝サーバ既定）。 |
| `[input]` | `enabled` / `host` / `port` / `token` / `seg_max_chars` / `seg_hard_max_chars` | ローカルWSテキスト入力口（AITuber/LLM連携・既定無効）。意味と契約は [`docs/USECASES.md`](docs/USECASES.md)。 |

### TTS プロファイル（`profiles/tts/*.toml`）

任意の TTS-API に対応するため、リクエストの組み立てと応答デコードを TOML で定義します（`[meta]`/`[request]`/`[response]`/`[audio]`）。`{{ text }}` が読み上げテキストに置換されます。例（GPT-SoVITS api_v2）は [`profiles/tts/gpt-sovits.toml`](profiles/tts/gpt-sovits.toml) を参照。別 TTS を使うときはこのファイルを複製・編集し、`server.toml` の `active_profile` で切り替えます。

---

## セキュリティ（外部公開する場合）

- **トークン必須**: `bind.host` を loopback 以外にすると `bind.token` が未設定なら**起動拒否**。クライアントは同じトークンを `Authorization: Bearer` で送ります。
- **TLS（推奨）**: `server.toml` に `tls_cert`/`tls_key` を設定して `wss://` 化。自己署名証明書の作り方・クライアント側 `tls_cafile`/`tls_insecure`・リバースプロキシ終端は [`docs/SETUP.md`](docs/SETUP.md) §9。
- トークンを平文 `ws://` の query に載せるとログに残りやすいため、既定では Bearer ヘッダで送信します（`token_in_query` は legacy 用）。

---

## 開発

```bash
uv sync --extra client --extra server   # 開発用に両 extra
uv run pytest -q                          # 全テスト（Qt は offscreen で自動実行）
```

- テストは外部サービス（GPT-SoVITS/Qwen/実マイク/GPU）不要で完結します（fake 注入）。
- 進め方・設計判断は `docs/IMPL-*.md`（実装計画）・`docs/REVIEW-codex-*.md`（レビュー指摘）・`docs/CODE-REVIEW-*.md`（検証記録）に記録。

---

## 設計意図（当初仕様）

> マイクで音声入力をし、テキストを入力。任意の TTS-API サーバに送信して音声を返す。
> - マイク・テキスト入力・API テキスト入力（LLM から送信する想定）に対応すること。
> - レイテンシ重視。
> - マイクは常時起動で、ノイズフィルタ、無音検知を行う。
> - TTS-API へのリクエストボディは複数の TOML で設定できるようにする（TTS ごとに受け入れリクエストボディが違うため）。
> - python(uv) & PySide でデスクトップアプリにする。
