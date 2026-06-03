# IMPL-DOCKER — サーバスタックの docker compose 化（GPT-SoVITS 同梱）

> 目的: **サーバ側（orchestrator + GPT-SoVITS）を `docker compose up` 一発**で立ち上げる。
> クライアント（PySide6 + マイク/スピーカーの Windows デスクトップGUI）は **Docker化しない**＝ネイティブのまま、WS で接続。
> ご指示「GPT-SoVITS も含めて compose に。client と server は分ける」に対応。

対象コミット基準: `dab7f63`。新規ファイルのみ追加（既存コードは原則 **無改変**）。

---

## 0. 確定した環境事実（調査済み）

| 項目 | 実体 |
|---|---|
| GPU | RTX 3090 Ti ×1 / 24GB / driver 590.44.01 (CUDA 13.1まで) |
| ホストツール | `docker`, `compose v5`, `nvidia-smi` あり |
| GPT-SoVITS | `/mnt/holyland/Project/GPT-SoVITS` に **重み一式同梱**（`GPT_SoVITS/pretrained_models/*`, `text/G2PWModel`）。独自 Dockerfile / docker-compose.yaml / プレビルドイメージ `xxxxrt666/gpt-sovits:latest-cu128` あり |
| GPT-SoVITS 起動 | `python api_v2.py -a <host> -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml`（既定 v2 / custom）。WORKDIR=`/workspace/GPT-SoVITS`, `PYTHONPATH=/workspace/GPT-SoVITS`（イメージ側 ENV） |
| orchestrator | `text-to-shaberi-server`（console_script）→ `uvicorn.run(host=bind.host, port=bind.port)`。`-c` で server.toml パス指定。既定 bind `127.0.0.1:8765` |
| STT | `QwenSTTEngine` を **lifespan 起動時**に `Qwen3ASRModel.from_pretrained("Qwen/Qwen3-ASR-1.7B")` で `cuda:0` ロード（in-proc, eager）。HF Hub 自動DL（~3.4GB） |
| TTS呼び出し | `TTSClient`(httpx) が profile の URL を叩く。profile = `profiles/tts/<active_profile>.toml`。URL/ref はここ。**コード上 env 展開なし** |
| profiles_dir 解決 | 空文字 → `Path(__file__).resolve().parents[3] / "profiles"`。＝ src レイアウトに依存（脆い）→ **docker では明示指定** |
| bind 検証 | `validate_bind`: 非loopback host で `token` 空なら **起動拒否**（ValueError） |
| codec | `opus`（既定）。opuslib は **libopus 実体が無いと不可** → pcm 自動フォールバック |
| Python | `requires-python >=3.12`。torch は `[[tool.uv.index]] cu128` 経由 |

---

## 1. 構成（トポロジ）

```
[Windows client (native, GUI/mic/speaker)]
        │  WS  ws://<linux-host-ip>:8765/ws  (token 必須)
        ▼
┌──────────────── docker compose (Linux GPU host) ─────────────────┐
│                                                                   │
│   service: orchestrator  ──HTTP──>  service: gpt-sovits           │
│   - uvicorn WS 0.0.0.0:8765         - api_v2  0.0.0.0:9880         │
│   - Qwen3-ASR を cuda:0 にロード    - GPT-SoVITS v2 を GPU 常駐    │
│   - publish 8765:8765               - publish しない(内部のみ)*   │
│        └──────── GPU 0 (RTX 3090 Ti 24GB) を共有 ────────┘        │
│                                                                   │
│   network: shaberi-net (bridge)  |  volume: hf-cache              │
└───────────────────────────────────────────────────────────────────┘
  * 9880 はデバッグ時のみ任意で publish
```

- **GPU 1枚共有**: Qwen(~3.4GB) + GPT-SoVITS v2(数GB) で 24GB に十分収まる。両サービス `runtime: nvidia` + `NVIDIA_VISIBLE_DEVICES=0`。
- **CUDA 系統そろえ**: orchestrator torch=cu128 / GPT-SoVITS イメージ=cu128 / driver=590(13.1) → 一致。
- orchestrator → GPT-SoVITS は **サービス名解決** `http://gpt-sovits:9880/tts`。

---

## 2. 新規ファイル一覧

| パス | 役割 | git |
|---|---|---|
| `docker-compose.yml`（repo直下） | 2サービス定義 | commit |
| `deploy/Dockerfile.server` | orchestrator イメージ（uv + cu128 torch + libopus0） | commit |
| `deploy/server.docker.toml.example` | docker用 server.toml ひな形（token 空） | commit |
| `deploy/server.docker.toml` | 実体（token入り） | **gitignore + mount** |
| `profiles/tts/gpt-sovits-docker.toml` | URL=サービス名 / ref=コンテナ内パス | commit |
| `deploy/.env.example` | compose 変数（publish port, GPU, パス） | commit |
| `.dockerignore`（repo直下） | ビルドコンテキスト削減 | commit |
| `docs/SETUP.md` / `README.md` 追記 | compose 手順 | commit |

> 既存 `server.toml.example` / `profiles/tts/gpt-sovits.toml`（ネイティブ用）は **そのまま残す**。docker 用は別ファイルで分離。

---

## 3. 各ファイル設計

### 3.1 `docker-compose.yml`（repo 直下）

```yaml
name: text-to-shaberi

services:
  gpt-sovits:
    image: xxxxrt666/gpt-sovits:latest-cu128
    # upstream CMD（rm pretrained_models → symlink）は bind-mount を破壊するため使わない。
    # 重みは下の bind-mount 済みローカルrepoに実在するので、api_v2 を直接起動する。
    working_dir: /workspace/GPT-SoVITS
    command: >
      bash -lc "python api_v2.py -a 0.0.0.0 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml"
    environment:
      - is_half=true
      - NVIDIA_VISIBLE_DEVICES=${GPU_ID:-0}
      - PYTHONPATH=/workspace/GPT-SoVITS
    volumes:
      - ${GPTSOVITS_DIR:-/mnt/holyland/Project/GPT-SoVITS}:/workspace/GPT-SoVITS
      # ref.wav は GPT-SoVITS が「自分のFS」から開く → read-only でマウント
      - ${SHABERI_ASSETS:-./assets}:/refs:ro
    shm_size: "16g"
    runtime: nvidia
    networks: [shaberi-net]
    restart: unless-stopped
    healthcheck:
      # api_v2 が listen し始めたら healthy。モデルロードに数分かかる想定で長め。
      test: ["CMD-SHELL", "python -c \"import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',9880))==0 else 1)\""]
      interval: 10s
      timeout: 5s
      retries: 60          # 最大 ~10分待つ
      start_period: 30s

  orchestrator:
    build:
      context: .
      dockerfile: deploy/Dockerfile.server
    command: ["text-to-shaberi-server", "-c", "/app/deploy/server.docker.toml"]
    environment:
      - NVIDIA_VISIBLE_DEVICES=${GPU_ID:-0}
      - HF_HOME=/hf
    volumes:
      - ./deploy/server.docker.toml:/app/deploy/server.docker.toml:ro
      - ./profiles:/app/profiles:ro          # profile を再ビルドなしで編集可能に
      - hf-cache:/hf                          # Qwen 重みを永続化
    ports:
      - "${WS_PORT:-8765}:8765"
    runtime: nvidia
    networks: [shaberi-net]
    depends_on:
      gpt-sovits:
        condition: service_healthy            # GPT-SoVITS が listen してから起動
    restart: unless-stopped

networks:
  shaberi-net:

volumes:
  hf-cache:
```

**判断ポイント / 要レビュー:**
- `command: bash -lc "python ..."`: conda base が PATH/bashrc で有効化される前提。**もし base が自動 activate されなければ** `conda run -n base python api_v2.py ...` に切替（要 `up` 実機確認）。← 「fake環境は実経路を通らない」教訓どおり、ここは**必ず実起動で検証**。
- `runtime: nvidia` は `/etc/docker/daemon.json` に nvidia runtime 登録が前提。未登録なら `deploy.resources.reservations.devices` 形式に切替（下記 §6 risk）。
- healthcheck の TCP 接続成功＝モデルロード完了とは限らない（listen が先の可能性）。`depends_on` は「クラッシュしてない」程度の保証。最初の speak で TTS 未準備なら 1 回失敗しうる → 許容 or `/` への HTTP healthcheck に強化を検討。

### 3.2 `deploy/Dockerfile.server`（orchestrator）

```dockerfile
# uv + Python 3.12。torch は pyproject の cu128 index から取得（wheel が CUDA runtime 同梱）。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# opuslib は libopus 実体を要求（無いと opus 不可→pcm fallback）。
RUN apt-get update \
 && apt-get install -y --no-install-recommends libopus0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存解決を層キャッシュに乗せる（lock + メタデータ先行コピー）
COPY pyproject.toml uv.lock README.md ./
# uv_build はビルド時に src を要求しうるため src も先に
COPY src ./src
RUN uv sync --extra server --no-dev --frozen

COPY profiles ./profiles
COPY deploy ./deploy

ENV PATH="/app/.venv/bin:${PATH}"
ENV HF_HOME=/hf

# console_script を直接起動（compose 側で command 上書き）
CMD ["text-to-shaberi-server", "-c", "/app/deploy/server.docker.toml"]
```

**要レビュー:**
- `uv sync --extra server` だが core 依存 `opuslib` も入る（opus に必要）。client extra は不要。
- base を slim にした根拠: torch cu128 の pip wheel は CUDA runtime を内包するので、CUDA ベースイメージ不要。GPU は `runtime: nvidia` がドライバ/デバイスを注入。**代替**: `nvidia/cuda:12.8.x-runtime` ベース（確実だが重い）。トレードオフは codex 判断を仰ぐ。
- `uv sync` 時に torch(cu128, 数GB)を取得 → 初回ビルド長め。`.dockerignore` でコンテキスト最小化。
- `uv_build` バックエンドのため editable ビルドに src が要る → COPY 順序に注意。

### 3.3 `deploy/server.docker.toml(.example)`

```toml
[bind]
host  = "0.0.0.0"          # コンテナ外（Windows）から接続するため
port  = 8765
token = "CHANGE_ME"        # 非loopback bind は token 必須（空だと起動拒否）。実体は gitignore
tls_cert = ""
tls_key  = ""

[stt]
backend  = "qwen_local"
language = "ja"
[stt.qwen_local]
model               = "Qwen/Qwen3-ASR-1.7B"
device              = "cuda:0"
dtype               = "bfloat16"
attn_implementation = "sdpa"

[tts]
profiles_dir   = "/app/profiles"     # parents[3] 解決に依存せず明示
active_profile = "gpt-sovits-docker" # ↓のサービス名向け profile

[transport]
codec = "opus"
opus_bitrate = 24000
```

### 3.4 `profiles/tts/gpt-sovits-docker.toml`

`gpt-sovits.toml` のコピーで **2点だけ差し替え**:

```toml
[request]
url = "http://gpt-sovits:9880/tts"   # 127.0.0.1 → compose サービス名

[request.body]
ref_audio_path = "/refs/ref.wav"     # GPT-SoVITS コンテナ内マウント先（assets→/refs:ro）
# prompt_text 等は据え置き
```

> `ref_audio_path` は **GPT-SoVITS 側のFS** を指す。orchestrator ではない。だから assets を `gpt-sovits` サービスへ `:ro` マウントし、その内側パスを書く。

### 3.5 `deploy/.env.example`

```dotenv
WS_PORT=8765
GPU_ID=0
GPTSOVITS_DIR=/mnt/holyland/Project/GPT-SoVITS
SHABERI_ASSETS=./assets
```

### 3.6 `.dockerignore`（repo 直下）

```
.git
**/__pycache__
*.pyc
.venv
.pytest_cache
docs
tests
*.md
deploy/server.docker.toml
```

### 3.7 `.gitignore` 追記

```
deploy/server.docker.toml
deploy/.env
```

---

## 4. 起動手順（README/SETUP 追記内容）

```bash
# 1. 設定を作る
cp deploy/server.docker.toml.example deploy/server.docker.toml
#   → token をランダム値に書き換え（例: openssl rand -hex 24）
cp deploy/.env.example .env
#   → GPTSOVITS_DIR / SHABERI_ASSETS を環境に合わせる

# 2. ビルド & 起動（初回は torch 取得 + Qwen DL + SoVITS ロードで数分）
docker compose build
docker compose up -d

# 3. ログでロード完了を確認
docker compose logs -f orchestrator   # [opus] available: True / uvicorn running
docker compose logs -f gpt-sovits     # api_v2 listening :9880

# 4. Windows クライアント
#    client.toml: server.address = ws://<linux-host-ip>:8765/ws, token=<同じ値>
```

---

## 5. 検証計画（実機 `up` で確認すべき項目）

> 教訓「fake/dev環境は実経路を通らない」。下記は **必ず実コンテナで** 確認。

1. `gpt-sovits` の `command` で conda base python が解決され api_v2 が :9880 で listen するか（`bash -lc` で足りるか／`conda run` 要るか）。
2. bind-mount 越しに `pretrained_models` / `G2PWModel` が読め、`tts_infer.yaml`(custom/v2) のモデルがロードされるか（**rm-rf 破壊が起きないこと**を最優先確認）。
3. orchestrator 起動時に Qwen が `cuda:0` にロードされ、`hf-cache` volume に重みが落ちて再起動で再DLされないか。
4. `[opus] available: True`（libopus0 が効いているか）。False なら Dockerfile 修正。
5. orchestrator → `http://gpt-sovits:9880/tts` のサービス名解決と疎通。
6. `validate_bind` を通過（token 設定済み）して 0.0.0.0:8765 で listen するか。
7. 別ホスト(Windows)から WS 接続 → STT→auto_tts→音声が返るE2E。
8. 24GB に両モデル同居で OOM しないか（`nvidia-smi`）。

---

## 6. リスクと対策

| リスク | 兆候 | 対策 |
|---|---|---|
| `runtime: nvidia` 未登録 | `unknown runtime` | `deploy.resources.reservations.devices`(`driver: nvidia`) 形式に切替 |
| conda 未 activate | api_v2 が `ModuleNotFoundError` | `command` を `conda run -n base ...` へ |
| upstream symlink CMD 誤適用 | ホスト重みが消える | 本計画は CMD 上書きで **symlink dance を実行しない**。万一のため作業前に重みを別途確認/バックアップ |
| Qwen 初回DLが遅い/失敗 | 起動が長い・HFレート制限 | `hf-cache` 永続化。必要なら事前 `huggingface-cli download` を volume へ |
| opus 不可 | `[opus] available: False` | `libopus0` 導入確認。最悪 `transport.codec="pcm"` |
| GPT-SoVITS 起動前に speak | 最初の1発が失敗 | `depends_on: service_healthy`。listen≠ロード完了なので HTTP healthcheck 強化検討 |
| torch cu128 と driver | CUDA error | driver 590=CUDA13.1 で cu128 は後方互換 OK（確認済み） |
| VRAM 競合 | OOM | 24GB に十分だが、将来大モデル化したら GPU 分離 or MIG |

---

## 7. スコープ外（今回やらない）

- クライアントの Docker 化（GUI/マイク/スピーカーの Windows アプリ。ご指示どおり分離・ネイティブ）。
- GPT-SoVITS のローカル Dockerfile ビルド（プレビルド `xxxxrt666/...:cu128` 流用に決定）。
- TLS/wss 終端（compose の前段 reverse proxy は別途。`server.docker.toml` の tls_* は空のまま）。
- マルチGPU / オートスケール。

---

## 8. 作業順

1. 上記新規ファイルを追加（既存無改変）。
2. codex(read-only) レビュー → 指摘反映（特に conda起動・nvidia runtime形式・healthcheck）。← **完了 (§9)**
3. `docker compose build` → `up -d` で §5 を実機検証。
4. 問題修正（conda起動方式・libopus 等を実測で確定）。
5. README/SETUP に手順追記。
6. branch → commit → `git merge --ff-only` main。

---

## 9. codex レビュー反映（v2 / 確定仕様）

codex(gpt-5.5, read-only) レビュー結果を実コードで裏取りのうえ反映。HIGH 2件は修正必須、MED/LOW は方針確定。**実ファイルは本節の確定仕様で作成する。**

### 9.1 HIGH① — GPT-SoVITS の conda 起動（確定）

`bash -lc "python ..."` は不安定（`conda init bash` は `~/.bashrc` 更新、login shell は `~/.bashrc` を直接 source しない）。conda は `Docker/miniforge_install.sh` より **`$HOME/conda` = `/root/conda`** に実在。確定 command:

```yaml
command:
  - bash
  - -lc
  - >
    source /root/conda/etc/profile.d/conda.sh && conda activate base &&
    exec python api_v2.py -a 0.0.0.0 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

> 万一 conda パスが異なる場合の確認: `docker run --rm xxxxrt666/gpt-sovits:latest-cu128 ls /root/conda/etc/profile.d`。

### 9.2 HIGH② — `.dockerignore` と README の矛盾（確定）

`pyproject.toml` の `readme = "README.md"` のため uv ビルドに `README.md` が必須。`.dockerignore` は `*.md` を消さず **`!README.md` 例外**を追加（docs は別途 `docs/` で除外）。

### 9.3 MED — GPU 予約形式（確定）

`runtime: nvidia`（daemon 登録依存）ではなく **`deploy.resources.reservations.devices`** を主とする。GPU_ID を `device_ids` で固定:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          device_ids: ["${GPU_ID:-0}"]
          capabilities: [gpu]
```

`NVIDIA_VISIBLE_DEVICES` env は二重指定回避のため落とす（device_ids がセレクタ）。`is_half` / `PYTHONPATH` / `HF_HOME` は残す。

### 9.4 MED — healthcheck（据え置き＋運用注記）

`api_v2.py` は `TTS_Config` / `TTS` 初期化後に `uvicorn.run` するため「listen≒ロード完了」で TCP healthcheck で概ね可。より堅くするなら別 readiness（`/tts` に固定文＋`/refs/ref.wav` を1回投げる）を運用スクリプトに追加（任意）。

### 9.5 MED/LOW — torch index scope / GPU共有（注記のみ・コード無改変）

- cu128 index が全依存に効く件: `tool.uv.sources` で torch のみに絞れるが、**動作中の `uv.lock` を触らない**方針につき今回は据え置き（任意改善）。
- 24GB 単一GPU共有: 妥当。同時 speak+STT のピークは実測。必要なら `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` を orchestrator env に追加（運用オプション）。
- ビルド時 smoke test は GPU 非搭載のため不可 → **実行時検証**へ（§5-3,4 + `docker compose run --rm orchestrator python -c "import torch;print(torch.cuda.is_available())"`）。

### 9.6 LOW — symlink 非使用の前提条件（明記）

「full repo に `GPT_SoVITS/pretrained_models/*` と `text/G2PWModel` が実在する場合のみ」bind-mount だけで成立（確認済み）。lite image や別 checkout では upstream の `/workspace/models` symlink 前提に戻るので、その場合は別途対応。

### 9.7 総合判定

中核方針は妥当。HIGH 2件を反映した本節の確定仕様で実ファイルを作成 → `docker compose build/up` の実機検証（§5）へ。
