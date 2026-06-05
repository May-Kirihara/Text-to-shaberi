# ユースケース別ガイド

Text-to-shaberi は同じクライアント／サーバで複数の使い方ができます。代表的な3つを、必要な設定と操作とともにまとめます。

| ユースケース | 入力 | 出力 | 主な設定 |
|---|---|---|---|
| ① **TTSフロントエンド** | GUI のテキスト欄 | 既定デバイス再生 | 既定のまま |
| ② **ボイスチェンジャー** | マイク（声→STT→TTS） | 仮想オーディオ → OBS等 | 出力=仮想シンク |
| ③ **AITuber（API受信）** | LLM等が**クライアントのローカルWS**へテキスト投入 | 仮想オーディオ → OBS／または `forward_audio` でプログラムが直接受信 | `[input]` 有効化 |

共通の前提（サーバ構築・起動）は [`README.md`](../README.md) / [`SETUP.md`](SETUP.md) を参照。話す“声”は **サーバ側 TTS プロファイル**（GPT-SoVITS の参照音声）で決まります。プロファイルは ⚙設定 または `client.toml` の `[profile].active_id` で選択。

> **本ガイドは全てクライアントアプリ経由の使い方（エンドユーザ向け）です。** 外部プログラム（LLM・OBS 等）が繋ぐ先は**クライアント**であり、サーバ（バックエンド）へ直接繋ぐ必要はありません。クライアント↔サーバ間の接続は設定済みの前提です。
> サーバへ直接つなぐ独自ソフトを作る**開発者向けの低レベル接続仕様**は [`PROTOCOL.md`](PROTOCOL.md) に分離しています（エンドユーザは読む必要はありません）。

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
   forward_audio = false   # true で「同じ接続に生成音声(pcm_s16le)も返す」（下記）。有効時はローカル再生を抑制
   seg_max_chars = 80      # 文分割の目安（これを超える長文は強制分割）
   seg_hard_max_chars = 160
   ```
2. **音声の出口を決める**（2通り。両方を実地で試せます）：
   - **(a) 配信に流す**：出力を仮想シンクに（②と同じ。OBS がそのデバイスを拾う）。LLM 側はテキストを送るだけ。
   - **(b) プログラムで音声を受け取る**：`forward_audio = true`（下記）。投入した同じ接続に音声バイトが返り、クライアントのローカル再生は抑制されます。
   - **(c) 別の外部エンジンへ配信**：口パクエンジン等に**全合成音声**を流すなら `[monitor]`（後述「音声モニタ」）。入力とは独立した専用ポートで broadcast し、ローカル再生は継続します。
3. クライアントを起動。ログに `ローカル入力口を起動: ws://127.0.0.1:8770` が出れば待受開始。
4. LLM パイプライン（別プロセス）から、**クライアントのローカルWS** `ws://127.0.0.1:<port>` に下記プロトコルでテキストを送る（サーバには繋ぎません）。

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

### 音声もクライアントから受け取る（`forward_audio`）

`[input].forward_audio = true` にすると、**テキストを投入した同じ接続に、生成音声（復号済み `pcm_s16le`）が返ってきます**。これにより「クライアントに入力 → クライアントから音声受信」が1接続で完結します（Opus でも復号後に返るので**消費側に Opus デコーダ不要**）。**この間クライアント自身のローカル再生は抑制**されます（純粋キャプチャ）。

受信シーケンス（テキスト送信後、同じ接続に追加で届く）:
```
← {"event":"accepted"}
← {"event":"audio_begin","format":{"codec":"pcm_s16le","sample_rate":N,"channels":C}}
← (binary) pcm_s16le フレーム × N
← {"event":"audio_end"}
← {"event":"done"}
```
- `sample_rate` は Opus 経路なら 48000、PCM 経路ならプロファイル依存。`channels` は通常 1。バイナリは連結して再生（PCM なのでメッセージ境界に意味なし）。
- テキストが空で音声が無い完了では `audio_*` は出ず、`done` のみ（消費側は `done` を終端とする）。

最小サンプル（音声を WAV 保存）:
```python
import asyncio, json, wave, websockets

async def speak_capture(text, out="out.wav", url="ws://127.0.0.1:8770"):
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"text": text, "final": True}, ensure_ascii=False))
        fmt, pcm = None, bytearray()
        while True:
            m = await ws.recv()
            if isinstance(m, bytes):
                pcm += m
            else:
                ev = json.loads(m)
                if ev.get("event") == "audio_begin":
                    fmt = ev["format"]
                elif ev.get("event") in ("done", "error"):
                    break
        if fmt:
            with wave.open(out, "wb") as w:
                w.setnchannels(fmt["channels"]); w.setsampwidth(2)
                w.setframerate(fmt["sample_rate"]); w.writeframes(bytes(pcm))

asyncio.run(speak_capture("こんにちは、世界。"))
```

> エンドユーザはこの `forward_audio`（クライアント経由）だけで完結します。サーバへ**直接**つなぐ独自ソフトを作る開発者向けの低レベル仕様は [`PROTOCOL.md`](PROTOCOL.md)（通常は不要）。

### 音声モニタ（口パク等へ全合成音声を配信）

「音声から口形を推論する口パクエンジン」など、**合成音声を別の外部プログラムへ流したい**用途の専用配信口です。`forward_audio`（テキスト投入元の1接続に返す）とは独立した、**全合成音声の broadcast**（複数購読可・テキスト投入不要・受信専用）。GUIテキスト・マイクVC・LLM入力など**クライアントが鳴らす音声すべて**が対象で、ローカル再生（OBS向け）はそのまま継続します。「WS に生えた仮想オーディオデバイス」のイメージです。

設定（`client.toml`）:
```toml
[monitor]
enabled = true        # 既定 false
host = "127.0.0.1"    # ローカル専用（非loopbackは token 必須・未設定なら自動無効化）
port = 8771           # 入力口(8770)とは別ポート
token = ""            # 任意の共有秘密。設定時は接続に必須
queue_max = 256       # 購読者ごとのバッファ（超過時は音声フレームのみ間引き）
```

受信（`ws://127.0.0.1:8771` に接続。送信不要・**受信専用**）:
```
← {"event":"audio_begin","format":{"codec":"pcm_s16le","sample_rate":N,"channels":C}}
← (binary) pcm_s16le フレーム × N
← {"event":"audio_end"}
（次の発話まで何も来ない。発話ごとに begin..end を繰り返す）
```
- `sample_rate` は Opus 経路 48000 / PCM 経路プロファイル依存。固定したいなら `[server].codec = "pcm"`。
- バイナリは連結再生（PCM、メッセージ境界に意味なし）。`audio_end` が発話の終端。
- 複数購読可。発話の途中で接続しても進行中なら `audio_begin` が補完されます。
- 消費が遅いと音声フレームが間引かれます（口パクは振幅ベースなので実用上問題なし）。begin/end は保証。全く受信しない購読者は切断されます。

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
