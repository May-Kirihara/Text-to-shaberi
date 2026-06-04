# ユースケース別ガイド

Text-to-shaberi は同じクライアント／サーバで複数の使い方ができます。代表的な3つを、必要な設定と操作とともにまとめます。

| ユースケース | 入力 | 出力 | 主な設定 |
|---|---|---|---|
| ① **TTSフロントエンド** | GUI のテキスト欄 | 既定デバイス再生 | 既定のまま |
| ② **ボイスチェンジャー** | マイク（声→STT→TTS） | 仮想オーディオ → OBS等 | 出力=仮想シンク |
| ③ **AITuber（API受信）** | LLM等が**ローカルWS**へテキスト投入 | 仮想オーディオ → OBS等 | `[input]` 有効化 |

共通の前提（サーバ構築・起動）は [`README.md`](../README.md) / [`SETUP.md`](SETUP.md) を参照。話す“声”は **サーバ側 TTS プロファイル**（GPT-SoVITS の参照音声）で決まります。プロファイルは ⚙設定 または `client.toml` の `[profile].active_id` で選択。

---

## ① TTSフロントエンドとして使う

入力したテキストをそのまま読み上げる、最もシンプルな使い方です。

1. クライアントを起動し、サーバに接続（ステータス行 `接続 ✓`）。
2. 下部のテキスト欄に文を入れて「送信」。「停止」で即中断。
3. 出力先は「出力デバイス」セレクタで既定／仮想シンク／任意デバイスを選択。

LLM やスクリプトから流し込みたい場合は ③ を参照（GUI テキスト欄は人間の手入力用です）。

---

## ② ボイスチェンジャーとして使う

自分の声をマイクで拾い、STT→TTS を通して**別の声で喋り直し**、その音声を仮想オーディオデバイス経由で OBS / Discord 等へ流します。

1. **出力を仮想シンクに**：⚙設定 → 出力デバイスで `TTS_Virtual`（配信用 null sink）を選択。OBS 側はその仮想デバイスをマイク入力としてキャプチャ。仮想シンクの作り方は [`SETUP.md`](SETUP.md)。
2. **声を選ぶ**：⚙設定 → プロファイルで目的の GPT-SoVITS 参照音声を選択。
3. **🎤 をON**：話して黙ると自動で区切られ、認識文が TTS で再生されます（再生中はハウリング防止でマイク一時停止）。
4. **喋りながら流したい**なら、メイン画面のモード切替で**ストリーミングモード**に。句の切れ目から順次・gapless に流れます。チューニングは [`STREAMING.md`](STREAMING.md)。

> STT を挟むため、原理的に「自分の発話 → 認識 → 合成」の遅延があります。最初の音までの体感は STREAMING.md のレイテンシ節を参照。

---

## ③ AITuber（LLM等からの API 受信）として使う

LLM が生成したテキストを**クライアントのローカル WebSocket 入力口**へ流し込み、文単位で先行合成して **1本の連続音声**として配信に流します。ライブ配信向けに、最初の文ができた時点で合成を開始します（全文完成を待たない）。

> 仕組みは「マイク版ストリーミング（speak_stream）の STT を抜いた版」です。詳細設計は [`IMPL-TEXTSTREAM.md`](IMPL-TEXTSTREAM.md)。

### セットアップ

1. **入力口を有効化** — `client.toml`：
   ```toml
   [input]
   enabled = true          # 既定は false（待受ソケットを開くので明示的に有効化）
   host = "127.0.0.1"      # ローカル専用。非loopbackにする場合は token 必須（未設定なら自動無効化）
   port = 8770
   token = ""              # 任意の共有秘密。設定時は接続に必須
   seg_max_chars = 80      # 文分割の目安（これを超える長文は強制分割）
   seg_hard_max_chars = 160
   ```
2. **出力を仮想シンクに**（②と同じ。OBS が拾う）。
3. クライアントを起動。ログに `ローカル入力口を起動: ws://127.0.0.1:8770` が出れば待受開始。
4. LLM パイプライン（別プロセス）から下記プロトコルでテキストを送る。

### ローカルWS プロトコル（公開契約）

- **接続**：`ws://127.0.0.1:<port>`（`token` 設定時は `?token=<t>` か `Authorization: Bearer <t>`）。
- **同時1接続**：v1 は単一アクティブ接続のみ。発話進行中の2本目は `{"event":"error","message":"busy"}` で弾かれます。
- **送信（JSON テキストフレーム）**：
  - `{"text": "…"}` — テキストを追記（**増分OK**。LLM のトークンストリームをそのまま流せます）。
  - `{"text": "…", "final": true}` — 追記して**この発話を確定**（残りを flush し連続音声を締める）。
  - `{"final": true}` — 確定のみ。
  - `{"type": "cancel"}` — 現在の発話を中断。
  - `profile` はストリーム先頭メッセージのみ尊重（声の切替）。接続切断＝確定扱い。
- **受信（JSON）**：
  - `{"event": "accepted"}` — 受領。
  - `{"event": "done"}` — 音声ストリーム完了（次の発話のペーシングに使える）。
  - `{"event": "error", "message": "…"}` — 失敗（`busy` / `busy: mic active` / 上流未接続 等）。

### 最小サンプル（Python）

```python
import asyncio, json, websockets

async def speak(sentences, url="ws://127.0.0.1:8770"):
    async with websockets.connect(url) as ws:
        for s in sentences:                 # LLM の文/トークンを順に
            await ws.send(json.dumps({"text": s}, ensure_ascii=False))
        await ws.send(json.dumps({"final": True}))
        # done が来るまで待つ（次の発話を詰めるなら）
        while True:
            ev = json.loads(await ws.recv())
            if ev.get("event") in ("done", "error"):
                print(ev)
                break

asyncio.run(speak(["こんにちは。", "今日はいい天気ですね。", "配信を始めます。"]))
```

LLM をストリーミングで使う場合は、トークン delta をそのまま `{"text": delta}` で投げ続け、応答の最後に `{"final": true}` を送れば、クライアント側が句点で区切って順次合成します。

### 低遅延のコツ（ライブ配信）

- **全文を待たず**、文/トークンを**届いた端から**送る（最初の文の句点で合成が始まる）。
- 定常状態の途切れなさは VAD ではなく**サーバの生成速度(RTF<1)**で決まります。GPU/TTS 負荷を抑える。
- localhost 運用なら `[server].codec = "pcm"` で Opus デコード分の遅延を省ける（帯域は増える）。
- 連続文の継ぎ目は gapless（1本の `audio_begin`/`audio_end`）。

### 注意・トラブルシュート

| 症状 | 原因 | 対処 |
|---|---|---|
| `{"event":"error","message":"busy: mic active"}` | マイク録音中は text 入力と**併用不可**（同じ合成スロット） | 🎤 をOFFにしてから送る |
| `{"event":"error","message":"start failed ..."}` | サーバ未接続／busy | サーバ接続を確認。`done` 後に次を送る |
| 2本目接続が `busy` | v1 は単一入力接続 | 1接続を使い回す |
| 起動ログに入力口が出ない | `[input].enabled=false`／非loopback+token空で自動無効化 | 設定を確認 |
| 文が長いまま切れない | 句点が無い長文 | `seg_max_chars`/`seg_hard_max_chars` を調整 |

> サーバが古く `text_stream` 未対応の場合は、**全文を一括 `speak` する fallback** で動作継続します（gapless 先行合成は効かない）。サーバを最新に。
