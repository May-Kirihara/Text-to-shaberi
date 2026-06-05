# WebSocket 接続仕様（サーバ。オーディオ受信ソフト向け）

GUI クライアントを介さず、**サーバの WebSocket に直接つないで合成音声ストリームを受け取りたい外部ソフト**（カスタムプレイヤー、録音/配信ブリッジ、AITuber ランタイム等）向けの接続仕様です。

> 対象は **サーバ**（`text-to-shaberi-server`）の `/ws` エンドポイント。クライアントのローカル入力口（テキスト投入）は別物で、[`USECASES.md`](USECASES.md) ③ を参照。
> なお「クライアントの文分割・低遅延パイプラインを使いつつ音声も1接続で受け取りたい」場合は、サーバ直結ではなくクライアントの `[input].forward_audio`（USECASES ③）が適します。
> 実装の正：[`src/text_to_shaberi/common/protocol.py`](../src/text_to_shaberi/common/protocol.py)（メッセージ型）／[`opus_codec.py`](../src/text_to_shaberi/common/opus_codec.py)（音声フォーマット）。

---

## 1. 接続

- **URL**: `ws://<host>:<port>/ws`（既定 `127.0.0.1:8765`）。TLS は `wss://`。
- **認証**: サーバに `bind.token` が設定されている場合、接続時に **`Authorization: Bearer <token>`** ヘッダが必須。
  - 互換として `?token=<token>` クエリも可（`token_in_query` 運用。平文ログに残りやすいので非推奨）。
  - `bind.host` が loopback 以外なら token は**必須**（未設定だとサーバが起動拒否）。
- **サブプロトコル**: なし。テキストフレーム＝JSON、バイナリフレーム＝音声。
- 1 接続 = 1 セッション。**同時にアクティブな合成要求は1つ**（処理中の追加要求は `busy` エラー）。

---

## 2. ハンドシェイク（必須・最初に1往復）

接続後、**最初のメッセージは必ず `hello`**。それ以外を送ると `error` を返して切断されます。

### → 送信 `hello`
```json
{
  "type": "hello",
  "protocol_version": 1,
  "client_name": "my-audio-consumer",
  "client_id": "",
  "caps": { "codecs": ["opus", "pcm_s16le"] }
}
```
- `caps.codecs`: 受信できるコーデックを優先順で。**`"pcm_s16le"` のみ**にすれば Opus デコード不要（実装が最小）。`"opus"` を含めると帯域を約 1/30 に圧縮（要 Opus デコーダ）。

### ← 受信 `ready`
```json
{
  "type": "ready",
  "session_id": "…",
  "server_time": 1717500000.0,
  "codec": "opus",
  "tts": { "codec": "pcm_s16le", "sample_rate": 32000, "channels": 1 },
  "profiles": [ { "id": "gpt-sovits", "name": "…" } ],
  "limits": { "max_chunk_bytes": 65536,
              "opus": { "available": true, "rate": 48000, "frame_ms": 20, "bitrate": 24000 } },
  "features": { "auto_tts": true, "speak_stream": true, "text_stream": true }
}
```
- **`codec`**: このセッションで実際に使われる音声コーデック（**ネゴ結果**）。`hello.caps` に `opus` があり、サーバ側 `transport.codec=opus` かつ Opus 利用可のとき `"opus"`、それ以外は `"pcm_s16le"`。
- `tts`: TTS のネイティブ PCM フォーマット（参考値）。実際の各ストリームのフォーマットは **`audio_begin.format` が正**。
- `profiles`: 利用可能な声（TTSプロファイル）一覧。要求時に `profile` で選択可。
- `features`: 利用可能な要求方式（§3）。

---

## 3. 音声を要求する

`ready` 受信後、次のいずれかで合成を要求します。いずれも応答は**同じ音声ストリーム**（§4）。

### 3.1 一発の読み上げ `speak`（最小・推奨スタート）
```json
{ "type": "speak", "id": "req-1", "text": "こんにちは、世界。", "profile": "gpt-sovits" }
```
- `id`: 任意の相関ID（必須・非空）。応答 `audio_begin`/`audio_end` の `id` に反映。
- `profile`: 省略可（省略時はサーバ既定の声）。
- 応答: `audio_begin(id) → (binary)×N → audio_end(id)`。

### 3.2 テキストストリーミング `text-stream`（低遅延・gapless / `features.text_stream` 必須）
LLM 等のテキストを**文が出来た端から**送り、**1本の連続音声**を得る方式。最初の文で生成が始まるので最初の音までが短い。
```json
{ "type": "stream_begin", "id": "s-1", "mode": "text", "auto_tts": true, "profile": "gpt-sovits" }
{ "type": "seg_text", "id": "s-1", "index": 0, "text": "こんにちは。" }
{ "type": "seg_text", "id": "s-1", "index": 1, "text": "今日はいい天気ですね。" }
{ "type": "stream_end", "id": "s-1" }
```
- `index` は 0 から昇順。サーバは順序を保って合成。
- 応答: **`audio_begin`/`audio_end` は各1回**、その間に連続して `binary`（文の継ぎ目で切れ目なし）。
- テキストが空/空白だけでも `audio_begin` 無しの **`audio_end` が必ず来る**（受信側は idle 復帰できる）。

### 3.3 （参考）音声入力→音声 `speak_stream` / 単発 STT
マイク音声を送って STT→TTS する方式（`stream_begin` の既定 `mode:"audio"` ＋ `seg_begin`/binary(PCM)/`seg_end`、または `stt_begin`/binary/`stt_end`）。**音声を送る側**の用途で、純粋な「音声受信」ソフトでは通常使いません。詳細は protocol.py 参照。

---

## 4. 音声ストリームの受信

要求への応答は **`audio_begin`（テキスト）→ バイナリフレーム×N → `audio_end`（テキスト）** です。

```json
{ "type": "audio_begin", "id": "req-1", "parent_id": null,
  "format": { "codec": "opus", "sample_rate": 48000, "channels": 1 } }
```
- `id`: 要求の `id`（`speak.id` / `stream_begin.id`）と一致。
- `parent_id`: 通常 `null`（auto_tts 連鎖時のみ STT id が入る）。
- `format`: **このストリームの音声フォーマット（権威）**。`codec` は `"opus"` か `"pcm_s16le"`。

`audio_begin` と `audio_end` の間に来る**バイナリWSフレーム**が音声本体。`audio_end` で1ストリーム終了。

### 4.1 Opus（`format.codec == "opus"`）
- **1 バイナリWSメッセージ = 1 Opus パケット**（長さ前置きなし。WS のメッセージ境界がパケット境界）。
- 各パケット = **48000 Hz / モノラル / 20 ms / 960 サンプル**（標準 Opus, RFC 6716）。ビットレートは `ready.limits.opus.bitrate`。
- 受信側はメッセージ毎に `decode(packet, 960)` → 1920 バイトの `pcm_s16le`(48k mono) を得て連結。**複数パケットを連結してから一括デコードしない**こと。

### 4.2 PCM（`format.codec == "pcm_s16le"`）
- バイナリフレームは生 PCM。**メッセージ境界に意味はなく、ただ連結**すればよい。
- フォーマットは `audio_begin.format`：**符号付き16bit・リトルエンディアン**、`sample_rate`（プロファイル依存・例 32000）、`channels`（多くは 1）。
- メッセージが奇数バイトで途切れる可能性に備え、末尾の半端バイトは次フレームへ繰り越して 2 バイト=1サンプルで解釈。

---

## 5. 中断・エラー

- **中断**: `{ "type": "cancel", "id": "<対象id>" }` を送る。
  - `speak` への中断 → `{ "type": "stopped", "id": … }`
  - `stream_begin`系（text/audio）への中断 → `{ "type": "stream_stopped", "id": … }`
- **エラー**: `{ "type": "error", "id": "<関連id|null>", "message": "…" }`。例: `busy: stream already active`（処理中に別要求）、プロファイル読込失敗、TTS失敗など。受信側は当該 `id` のストリームを破棄。

---

## 6. メッセージ・リファレンス

| 方向 | type | 主なフィールド | 備考 |
|---|---|---|---|
| C→S | `hello` | `protocol_version`,`caps.codecs[]` | 最初に必須 |
| S→C | `ready` | `codec`,`tts`,`profiles[]`,`limits`,`features` | ハンドシェイク応答 |
| C→S | `speak` | `id`,`text`,`profile?` | 一発読み上げ |
| C→S | `stream_begin` | `id`,`mode`("audio"/"text"),`auto_tts`,`profile?` | ストリーム開始 |
| C→S | `seg_text` | `id`,`index`,`text` | text-stream のテキスト片 |
| C→S | `seg_begin`/`seg_end` | `id`,`index`,`format?` | audio-stream の音声片（間に binary） |
| C→S | `stream_end` | `id` | ストリーム確定 |
| C→S | `cancel` | `id` | 中断 |
| S→C | `audio_begin` | `id`,`parent_id`,`format{codec,sample_rate,channels}` | 音声開始 |
| S→C | *(binary)* | — | Opus=1msg1packet / PCM=連結 |
| S→C | `audio_end` | `id` | 音声終了 |
| S→C | `transcript` | `id`,`index`,`text`,`final` | STT 経路のみ（text-stream では出ない） |
| S→C | `stopped` / `stream_stopped` | `id` | 中断確定 |
| S→C | `error` | `id?`,`message` | エラー |

---

## 7. 最小受信実装（Python・PCM 要求でOpusデコード不要）

```python
import asyncio, json, wave
import websockets

async def synth_to_wav(text, out="out.wav", url="ws://127.0.0.1:8765/ws", token=""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with websockets.connect(url, additional_headers=headers) as ws:
        # 1) hello（PCM のみ要求 → Opus デコーダ不要）
        await ws.send(json.dumps({"type": "hello", "protocol_version": 1,
                                  "caps": {"codecs": ["pcm_s16le"]}}))
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready

        # 2) 合成要求
        await ws.send(json.dumps({"type": "speak", "id": "req-1", "text": text},
                                 ensure_ascii=False))

        # 3) 音声受信
        fmt, pcm = None, bytearray()
        async for raw in ws:
            if isinstance(raw, bytes):
                pcm += raw                      # PCM はメッセージ境界を無視して連結
            else:
                m = json.loads(raw)
                if m["type"] == "audio_begin":
                    fmt = m["format"]           # {codec, sample_rate, channels}
                elif m["type"] == "audio_end":
                    break
                elif m["type"] == "error":
                    raise RuntimeError(m["message"])

        # 4) WAV 書き出し
        with wave.open(out, "wb") as w:
            w.setnchannels(fmt["channels"]); w.setsampwidth(2)
            w.setframerate(fmt["sample_rate"]); w.writeframes(bytes(pcm))
    print("wrote", out, fmt)

asyncio.run(synth_to_wav("こんにちは、世界。"))
```

**Opus を受ける場合**は `caps.codecs=["opus","pcm_s16le"]` とし、`audio_begin.format.codec=="opus"` のとき各バイナリメッセージを1パケットとしてデコード:
```python
import opuslib
dec = opuslib.Decoder(48000, 1)
# バイナリメッセージ毎に:
pcm48 = dec.decode(msg, 960)   # → 1920 バイト pcm_s16le @48k mono
```

---

## 8. 注意点（まとめ）

- ハンドシェイク（hello→ready）を**必ず**先に。`ready.codec` と各 `audio_begin.format` でフォーマットを確定。
- **Opus はWSメッセージ境界がパケット境界**（連結デコード禁止）。PCM は連結でよい。
- PCM は **s16le・mono が基本**、`sample_rate` はプロファイル依存（例 32000、Opus は常に 48000）。
- 1 接続で**同時1要求**。`busy` が返ったら前要求の `audio_end`/`stopped` を待つ。
- 低遅延が要るなら §3.2 の text-stream（先頭文で生成開始・gapless）。体感は生成 RTF<1 が前提。
