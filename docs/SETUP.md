# Text-to-shaberi 環境構築手順書（v1）

実装(Spike/M0)に入る前に、外部依存（**GPT-SoVITSサーバ** と **Qwen3-ASR** と **仮想オーディオ**）を整える手順。
このマシン向けに具体化済み。各 Part の末尾に**動作確認**を置く。最後の §5 が通れば実装へ進める。

## 0. このマシンの前提（確認済み 2026-06-03）

| 項目 | 値 |
|------|----|
| OS | Ubuntu 24.04.3 LTS |
| GPU | NVIDIA RTX 3090 Ti / 24GB / driver 590.44.01 |
| CUDA | 12.9 (nvcc V12.9.86) |
| Python | 3.12.3（system）／conda 25.9.1 あり |
| ツール | uv 0.9.16 / git 2.43 / ffmpeg 6.1.1 |
| 音声 | PipeWire 1.0.5（`pactl` は未導入 → §4で入れる） |
| 空き容量 | `$HOME` に 621GB（十分） |

## 1. 全体構成（2プロセス）

```
┌─ プロセスA: GPT-SoVITS api_v2 サーバ ─┐   ┌─ プロセスB: Text-to-shaberi アプリ ─┐
│ conda env (python 3.10)               │   │ uv env (python 3.12)                  │
│ HTTP :9880 /tts                       │◄──┤ httpx で叩く（TTS）                   │
│ GPUにTTSモデル常駐                     │   │ qwen-asr でSTTモデルをGPU常駐(in-proc)│
└───────────────────────────────────┘   │ PySide6 / sounddevice / FastAPI       │
        ▲ 両プロセスが cuda:0 を共有 ◄────────┘ → 仮想オーディオ/既定デバイスへ出力   │
                                             └───────────────────────────────────┘
```

- **GPT-SoVITS は独立サーバプロセス**（専用 conda env）。アプリとは HTTP で疎結合。
- **Qwen3-ASR はアプリ内(uv env)でロード**しGPU常駐。
- 両者が同一GPUを使うため、§5の **Spike-C で同時常駐VRAMを実測**する（計画§5.1の検証ゲート）。

> 配置案：外部依存はアプリrepoの外に置く。例 `/mnt/holyland/Project/GPT-SoVITS`（このrepoは `/mnt/holyland/Project/Text-to-shaberi`）。参照音声はアプリ側 `assets/` に置く。

---

## 2. 共通の前提パッケージ

```bash
sudo apt update
sudo apt install -y build-essential git-lfs pulseaudio-utils libportaudio2
git lfs install
```
- `build-essential`＝flash-attn等のビルド用、`git-lfs`＝モデル取得、`pulseaudio-utils`＝`pactl`（仮想オーディオ）、**`libportaudio2`＝sounddevice/PortAudio のネイティブlib（必須）**。

---

## 3. Part A — GPT-SoVITS api_v2 サーバ

### 3.1 取得とインストール
```bash
cd /mnt/holyland/Project
git clone https://github.com/RVC-Boss/GPT-SoVITS.git
cd GPT-SoVITS

conda create -n GPTSoVits python=3.10 -y
conda activate GPTSoVits

# CUDA 12.9 ドライバなので CU128 ホイールを使用、モデルはHFから取得
bash install.sh --device CU128 --source HF
```
- `install.sh` が依存＋事前学習モデルを取得する。失敗時は手動：
  ```bash
  pip install -r extra-req.txt --no-deps
  pip install -r requirements.txt
  ```
  事前学習モデルは Hugging Face から取得し `GPT_SoVITS/pretrained_models` に配置。

### 3.2 参照音声を用意（ゼロショット）
- 出したい声の **クリーンな約5秒の wav** を1つ用意し、`/mnt/holyland/Project/Text-to-shaberi/assets/ref.wav` に置く。
- その**書き起こしテキスト**（=`prompt_text`）と**言語**（`ja`）を控える。tomlプロファイルに使う。

### 3.3 サーバ起動
```bash
conda activate GPTSoVits
cd /mnt/holyland/Project/GPT-SoVITS
python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```
- 既定で `127.0.0.1:9880`。モデルの初回ロードに時間がかかる。

### 3.4 ✅ 動作確認（= Spike-A の素地）
別ターミナルで：
```bash
curl -s -X POST http://127.0.0.1:9880/tts \
  -H "Content-Type: application/json" \
  -d '{
    "text":"これはテストです。",
    "text_lang":"ja",
    "ref_audio_path":"/mnt/holyland/Project/Text-to-shaberi/assets/ref.wav",
    "prompt_text":"ある条件を満たすと、追ってくるモンスターを逆襲して食べることができる。",
    "prompt_lang":"ja",
    "media_type":"wav",
    "streaming_mode":false
  }' --output /tmp/tts_test.wav
ffplay -autoexit -nodisp /tmp/tts_test.wav   # or: aplay /tmp/tts_test.wav
```
- `/tmp/tts_test.wav` が再生できれば OK。次に `media_type":"raw"` と `streaming_mode":true` での挙動・`Content-Type`・**最初のチャンク到達時間**を確認（§5 Spike-A）。

---

## 4. Part B — 仮想オーディオデバイス（PipeWire）

OBS/Discord へ流すための仮想シンクを作る（出力先「両方切替」の片側）。

```bash
# 仮想シンク(null sink)を作成。アプリの出力先に "TTS_Virtual" を選べるようになる
pactl load-module module-null-sink \
  sink_name=ttsvirt \
  sink_properties=device.description=TTS_Virtual

# 確認
pactl list short sinks            # ttsvirt が見えるはず
wpctl status | grep -i tts        # WirePlumber 側でも確認
```
- OBS/Discord 側は入力に **「Monitor of TTS_Virtual」** を選ぶと、アプリ音声が流れる。
- **永続化**：再起動で消える。常用するなら `~/.config/pipewire/pipewire-pulse.conf.d/` に null-sink 定義を置くか、ログイン時に上記 `pactl load-module` を走らせる（systemd user サービス等）。MVP段階は手動でも可。
- 解除：`pactl unload-module module-null-sink`（または module 番号指定）。

> アプリ(sounddevice/PortAudio)はPipeWireのsinkをデバイスとして列挙する。§実装では「既定」「TTS_Virtual」をGUIで切替。

---

## 5. Part C — Text-to-shaberi アプリ env（uv）＋ Qwen3-ASR

### 5.1 uv プロジェクト初期化
```bash
cd /mnt/holyland/Project/Text-to-shaberi
uv init --package --python 3.12        # pyproject.toml を作成（パッケージ構成）
```

### 5.2 依存追加
```bash
# PyTorch（CUDA 12.8 ホイール。driver 590 で動作）
uv add torch --index https://download.pytorch.org/whl/cu128

# STT / TTSクライアント / GUI / 音声 / API
uv add qwen-asr                         # ローカルQwen3-ASR（transformers backend）
uv add PySide6 qasync sounddevice soxr numpy httpx fastapi uvicorn
uv add silero-vad                       # VAD（webrtcvad-wheels をフォールバックに）

# 任意：高速化。重いCUDAビルド＆torch依存で失敗しやすい → 基本は飛ばしてsdpaで進める。
# どうしても入れるなら、--no-build-isolation 用にビルド依存を先に入れてから：
#   uv pip install wheel packaging ninja
#   uv add flash-attn --no-build-isolation
```

### 5.3 Qwen3-ASR モデル取得（任意：事前DL）
```bash
# 自動DLされるが、事前に落とすなら：
uv run huggingface-cli download Qwen/Qwen3-ASR-1.7B
```

### 5.4 ✅ 動作確認（= Spike-B の素地）
`/tmp/stt_check.py`：
```python
import time, torch
from qwen_asr import Qwen3ASRModel

t0 = time.time()
model = Qwen3ASRModel.from_pretrained(
    "Qwen/Qwen3-ASR-1.7B",
    dtype=torch.bfloat16,
    device_map="cuda:0",
    attn_implementation="sdpa",  # 既定。flash-attn導入済みなら "flash_attention_2" で高速化
    max_new_tokens=256,
)
print(f"load: {time.time()-t0:.1f}s")

t1 = time.time()
r = model.transcribe(audio="/tmp/tts_test.wav", language="Japanese")
print(f"stt: {time.time()-t1:.2f}s  text={r[0].text!r}")
```
```bash
uv run python /tmp/stt_check.py
```
- 文字起こしが返れば OK。`stt` 時間が **Spike-B（STT遅延）** の実測値。

---

## 6. 仕上げの確認（Spike まとめ＝計画§11の前倒しPoC）

| Spike | 内容 | 合否の見方 |
|-------|------|-----------|
| **A** | GPT-SoVITS `/tts` raw + streaming の **first-chunk到達時間 / sample_rate** 実測 | TTFA目標(§3: p50≤400ms)に届くか／rawのrate確定 |
| **B** | Qwen3-ASR 単発の **STT処理時間**（日本語） | マイク往復(§3: p50≤1.2s)の内訳に収まるか |
| **C** | GPT-SoVITSサーバ常駐 **＋** Qwenロード時の **VRAM同時使用量** | `nvidia-smi` で 24GB に収まるか／OOMしないか |

Spike-C 簡易計測：
```bash
# 両方をロードした状態で
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

---

## 7. トラブルシュート

- **GPT-SoVITS起動で `OSError: libcudart.so.13 ...`（torchaudio）** → `install.sh` が torch を `+cu128`、torchaudio/torchvision を PyPIデフォルト(CUDA13ビルド)で入れてしまう版差。torch本体(2.11.0+cu128)に合わせて入れ直す：
  ```bash
  ENV=/home/misaki_maihara/miniconda3/envs/GPTSoVits
  "$ENV/bin/pip" install --index-url https://download.pytorch.org/whl/cu128 \
    --force-reinstall --no-deps torchaudio==2.11.0 torchvision==0.26.0
  ```
  3つとも `+cu128` に揃い、`ldd .../_torchaudio.abi3.so` が `libcudart.so.12` を参照すればOK。
- **flash-attn のビルド失敗**（`ModuleNotFoundError: No module named 'wheel'` 等）→ **飛ばしてOK**。`attn_implementation="sdpa"`（または `"eager"`）で動く。`--no-build-isolation` はビルド環境に `wheel/packaging/ninja/torch` が要るため、入れるなら先に `uv pip install wheel packaging ninja`。RAM不足なら `MAX_JOBS=4`。
- **torch が CUDA を認識しない** → `uv run python -c "import torch;print(torch.cuda.is_available(), torch.version.cuda)"` で確認。`cu128` ホイールを使っているか確認。
- **VRAM OOM（Spike-C）** → GPT-SoVITSのモデル軽量化／Qwenを必要時のみロード／最終手段で STT をクラウド(DashScope)へ切替（計画§5.3）。
- **PipeWireに `pactl` が無い** → `sudo apt install pulseaudio-utils`（§2で導入済みのはず）。
- **GPT-SoVITS `streaming_mode` で詰まる** → 版差あり。`false`→`true`→`3` の順で疎通確認し、効くものをtomlに採用（計画§7.1）。

---

## 8. 完了条件 → 実装へ

- [ ] §3.4：GPT-SoVITS `/tts` で wav が鳴る
- [ ] §4：`TTS_Virtual` シンクが見える
- [ ] §5.4：Qwen3-ASR で日本語の文字起こしが返る
- [ ] §6：Spike A/B/C の実測値を `docs/PLAN.md §3` の暫定SLOへ反映

これらが通ったら **M0（uvプロジェクト整備＋PySide6空窓＋設定/プロファイルローダ）** から実装着手。
