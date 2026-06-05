# IMPL-AUDIOTAP — クライアント経由「テキスト入力 ↔ 音声受信」ラッパー

> 目的: ローカル入力口（`LocalInputServer`）を拡張し、**テキストを投入した同じ接続で、生成音声（復号済み `pcm_s16le`）も受け取れる**ようにする。これにより外部プログラムは「クライアントに text in → クライアントから audio out」を1接続で完結でき、クライアントの `TextSegmenter`（自動文分割）・低遅延パイプライン・相関(done) を活かしたまま音声バイトを取得できる。
>
> 確定方針（2026-06-05, May）: ① β＝クライアントにラッパー実装。② **ローカル再生は抑制**（forward_audio 有効時は音声を入力元プログラムへ返すだけ＝純粋キャプチャ）。③ 音声は **復号済み `pcm_s16le`**（消費側に Opus デコーダ不要）。

関連: [[aituber-textstream-feature]] / `docs/IMPL-TEXTSTREAM.md` / `docs/USECASES.md`(③) / `docs/PROTOCOL.md`

---

## 0. 現状と差分

- 現状：ws_client は受信音声を復号して **PlaybackEngine で再生するだけ**（`_on_binary` line ~631 で復号済み `pcm` を得て engine/sink へ feed）。入力元プログラムへは返らない。
- 差分：ws_client に **audio tap**（観測コールバック）と **capture フラグ**を追加。capture stream では engine 再生をスキップし、復号 pcm を tap へ。app が tap を `LocalInputServer` へ繋ぎ、入力接続へ `audio_begin`/binary/`audio_end` を返す。
- **scope**: capture は「ローカル入力口が起こした stream のみ」。GUI テキスト送信・マイクは capture=False＝従来どおり再生（影響なし）。mic と text 入力は元々排他。

---

## 1. ws_client.py — audio tap ＋ capture フラグ

```python
# __init__: 追加
self._on_audio: Callable[[str, Any], None] | None = on_audio  # ("begin",fmt_dict)/("chunk",pcm:bytes)/("end",None)
self._capture: bool = False

# speak / start_text_stream に capture 引数追加（既定 False）
async def speak(self, text, profile=None, capture=False): ...
async def start_text_stream(self, auto_tts=True, profile=None, capture=False): ...
#   → 各 start 時に self._capture = capture

# _finish_stream(): リセットに self._capture = False を追加
```

### AudioBegin ハンドラ（capture 分岐）
復号器/codec の準備は capture でも**必要**（tap へ pcm を渡すため）。差分は **engine.begin をスキップ**して tap に begin を通知する点のみ。

```python
# _playback_id, fmt 決定は現状どおり。codec 準備:
if fmt.codec == OPUS_CODEC_NAME:
    self._playback_codec = OPUS_CODEC_NAME
    self._opus_decoder = OpusStreamDecoder()
    decoded_fmt = {"codec": "pcm_s16le", "sample_rate": 48000, "channels": 1}
else:
    self._playback_codec = "pcm_s16le"
    self._opus_decoder = None
    decoded_fmt = {"codec": "pcm_s16le", "sample_rate": fmt.sample_rate, "channels": fmt.channels}

if self._capture:
    if self._on_audio:
        self._on_audio("begin", decoded_fmt)        # engine.begin はしない＝ローカル再生抑制
else:
    # 現状の engine/sink.begin(...) ブロックをそのまま実行（通常再生）
```

### _on_binary（capture 分岐）
復号は共通。capture なら **engine.feed をスキップして tap へ**。TTFA メトリクスは両方で算出。

```python
# 復号（既存）: pcm = decode(raw) or raw
if self._capture:
    if self._on_audio:
        self._on_audio("chunk", pcm)
    # 先頭チャンク TTFA ログは従来どおり
else:
    sink = self._engine or self._sink
    ok = sink.feed(pcm)
    ...（既存のオーバーロード処理）
```

### AudioEnd ハンドラ（capture 分岐）
capture では engine.end/sink.end は不要（begin していない）。`_finish_stream` 前に tap end。

```python
if msg.id == self._playback_id:
    if self._capture:
        if self._on_audio:
            self._on_audio("end", None)
        self._finish_stream()
    elif self._cancelling:
        self._finish_stream()
    else:
        engine.end()/sink.end(); self._finish_stream()
elif msg.id == self._active_id and self._playback_id is None:
    self._finish_stream()   # 空ストリーム(§14.1)。capture でも音声無し＝tap無しでよい
```

> 注意: 空ストリーム（audio_begin 無し→audio_end）では tap begin/end を出さない（消費側は `done` で完了を知る）。または begin 無しの end のみ通知。v1 は **begin が出た stream のみ end を出す**（対称性）。

---

## 2. text_stream_bridge.py — capture 伝達

```python
# __init__ に capture: bool = False を追加し self._capture = capture
# StreamStart 処理: sid = await self.ws.start_text_stream(profile=self._profile, capture=self._capture)
# fallback: await self.ws.speak(text, self._profile, capture=self._capture)
```

---

## 3. text_input.py（LocalInputServer）— 音声返し（順序保証）

tap は loop スレッドから同期的に呼ばれる。順序保証のため **内部キュー＋単一 sender** で active 接続へ送る。

```python
# __init__: self._audio_q = asyncio.Queue(); self._audio_task = None
# start(): self._audio_task = asyncio.ensure_future(self._audio_sender())
# close(): self._audio_task.cancel()（await 例外握り）＋既存 server close

def send_audio(self, kind: str, payload) -> None:   # 同期・tapから呼ぶ
    # kind: "begin"(payload=fmt dict) / "chunk"(payload=bytes) / "end"(payload=None)
    self._audio_q.put_nowait((kind, payload))

async def _audio_sender(self) -> None:
    while True:
        kind, payload = await self._audio_q.get()
        conn = self._active_conn
        if conn is None:
            continue
        try:
            if kind == "begin":
                await conn.send(json.dumps({"event": "audio_begin", "format": payload}, ensure_ascii=False))
            elif kind == "chunk":
                await conn.send(payload)              # binary フレーム
            elif kind == "end":
                await conn.send(json.dumps({"event": "audio_end"}))
        except Exception:
            pass
```

- バイナリ送出は `conn.send(bytes)`。テキスト/制御と同一接続で交互に流れる。
- `_active_conn` が無い/変わった場合は drop（best-effort）。

---

## 4. config.py — `[input].forward_audio`

```python
# InputConfig に追加
forward_audio: bool = False
# load 時の追加クランプ不要（bool）
```

---

## 5. app.py — 配線

```python
# WSClient 構築に on_audio を渡す。ただし音声→入力口は forward_audio 時のみ。
def _on_audio(kind, payload):
    if local_input_server is not None:
        local_input_server.send_audio(kind, payload)
ws = WSClient(..., on_audio=_on_audio)   # 既存引数に追加

# TextStreamBridge 構築に capture=cfg.input.forward_audio を渡す
text_stream_bridge = TextStreamBridge(ws, ..., capture=cfg.input.forward_audio)
```

- `on_audio` は ws_client が **capture stream のときだけ**発火するので、GUI speak/mic 音声は流れない（_on_audio が呼ばれない）。`local_input_server is None`（input無効）なら no-op。

---

## 6. ローカルWS 音声出力プロトコル（契約・USECASES/PROTOCOL に転記）

`forward_audio=true` のとき、テキスト投入した接続に次が**追加で**流れる：
```
（→ {"text":…}/{"final":true} 送信後）
← {"event":"accepted"}
← {"event":"audio_begin","format":{"codec":"pcm_s16le","sample_rate":N,"channels":C}}
← (binary) pcm_s16le フレーム × N
← {"event":"audio_end"}
← {"event":"done"}
```
- 音声は **常に復号済み `pcm_s16le`**（Opus でも復号後）。`sample_rate` は Opus 経路なら 48000、PCM 経路ならプロファイル依存。`channels` は通常 1。
- バイナリフレームは連結して再生（PCM なのでメッセージ境界に意味なし）。
- `forward_audio=false`（既定）では音声は返らず、クライアントが従来どおりローカル再生。

---

## 7. テスト

- `test_ws_textstream.py` 追記（client側・fake server）：
  - capture=True の start_text_stream で server が AudioBegin+binary+AudioEnd を返したとき、**engine.begin/feed が呼ばれず**（FakeEngine.records 空）、**on_audio が begin/chunk/end の順で発火**し、chunk が復号 pcm であること。
  - capture=False では従来どおり engine 再生・on_audio 未発火。
- `test_text_stream_bridge.py` 追記：capture=True で `start_text_stream(capture=True)`／fallback `speak(..., capture=True)` が呼ばれる。
- `test_local_input_server.py` 追記：`send_audio("begin"/"chunk"/"end")` が active 接続へ `audio_begin`(JSON)→binary→`audio_end`(JSON) の順で届く。
- `test_client_config.py` 追記：`[input].forward_audio` ロード。

---

## 8. 受け入れ条件

1. `[input].forward_audio=true` 起動で、ローカル入力口にテキスト投入すると、**同じ接続に** `audio_begin`→binary(pcm_s16le)→`audio_end`→`done` が返る。
2. その間 **クライアントのローカル再生は行われない**（capture＝engine.begin/feed スキップ）。
3. `forward_audio=false`（既定）では音声は返らず従来どおりローカル再生。GUI/mic は forward_audio に関係なく常にローカル再生（capture=False）。
4. Opus 経路でも消費側は pcm_s16le をそのまま受ける（48000/mono）。
5. pytest 緑＋ruff クリーン＋offscreen 実起動smokeで begin/chunk/end 相関を実証。

---

## 9. GLM委託の割り（ファイル単位）

1. `ws_client.py`（on_audio/capture・AudioBegin/_on_binary/AudioEnd 分岐・speak/start_text_stream capture 引数・_finish_stream リセット）＋ `test_ws_textstream.py` 追記
2. `text_stream_bridge.py`（capture 伝達）＋ `test_text_stream_bridge.py` 追記
3. `text_input.py`（LocalInputServer.send_audio＋_audio_sender＋close）＋ `test_local_input_server.py` 追記
4. `config.py`（forward_audio）＋ `test_client_config.py` 追記
5. `app.py`（on_audio 配線・bridge capture）
6. ドキュメント（USECASES.md ③・PROTOCOL.md に「クライアント経由 audio out」節）

---

## 10. codexレビュー反映（v2・確定。本節が §1〜§9 を上書きする）

codex(gpt-5.5) 総評: **条件付きGO**。下記を反映して実装する。

### 10.1 [HIGH#1] capture は bool ではなく `_capture_id`（stream id 紐付け）
- ws_client に `_capture_id: str | None = None`、`_tap_open: bool = False`。
- `speak`/`start_text_stream` は `capture` 引数を受け、**`effective = capture and (self._on_audio is not None)`**（[MED#4]）。**start 成功後**（_active_id 確定後）に `self._capture_id = stream_id`。送信例外時は設定しない。
- 判定ヘルパ `_is_capture()` = `self._capture_id is not None and self._active_id == self._capture_id`。
- これで GUI speak(capture外) の無音化／local 音声の再生漏れを構造的に防ぐ（グローバル bool のリセット漏れ事故を排除）。

### 10.2 [HIGH#4] tap の `audio_end` は `_finish_stream()` に集約（全終了経路で1回だけ）
- AudioBegin(capture) → `on_audio("begin", decoded_fmt)` ＋ `_tap_open=True`（**engine.begin はしない＝ローカル再生抑制**）。
- **`_finish_stream()` の冒頭で**: `if self._tap_open and self._on_audio: self._on_audio("end", None)`、続けて `self._tap_open=False; self._capture_id=None`。
- 通常 AudioEnd / Stopped / StreamStopped / Error / decode error / cancel / `_recv_loop finally` 切断は**全て `_finish_stream()` を通る**ので end が必ず出る。AudioEnd ハンドラ等は capture でも end を**個別送信しない**（二重防止）。

### 10.3 [HIGH#2/#3] LocalInputServer の送信を「単一 outbound queue ＋接続タグ」に統一
- **アクティブ接続向けの全 outbound（accepted / audio_begin / audio_chunk / audio_end / done / error）を1本の `_out_q` ＋単一 `_sender` タスクで順序化**（`audio_end` と `done` の順序乱れを解消）。各 item は `(conn, kind, payload)`。
- sender は `item.conn is self._active_conn` を確認し、不一致なら drop（**旧接続の残留音声が新接続へ漏れない**）。
- **handshake 期の拒否**（unauthorized / busy 2本目接続）は従来どおり**直接 close**（_out_q を介さない・対象 conn 限定）。
- `_out_q` は **bounded**（maxsize 例 512）。満杯時 `audio_chunk` は drop（throttled warn）。`begin/end/accepted/done/error` の制御は優先（満杯なら最古の chunk を1つ捨てて空ける）。メモリ無制限化を防止。
- メソッド整理: `enqueue(conn, kind, payload)`（同期 put_nowait）。`notify(event)`→`enqueue(active, "json", event)`。`send_audio(kind,payload)`→`enqueue(active, kind, payload)`。`_handle` のアクティブ ack も enqueue。
- `start()` で `_sender` 起動、`close()` で cancel（await 例外握り）。

### 10.4 [MED#1] fmt None / 未知 codec を明示処理
- AudioBegin: `fmt = msg.format or (self._ready.tts if self._ready else None)`。`fmt is None` または `fmt.codec not in (OPUS_CODEC_NAME, "pcm_s16le")` → `on_audio` せず `error` 状態＋`_finish_stream()`。

### 10.5 [MED#2] `_on_binary` は復号直後に capture 早期 return
```python
# pcm を復号した直後（sink 取得の前）:
if self._is_capture():
    if self._on_audio:
        self._on_audio("chunk", pcm)
    <既存 TTFA 計測ブロック>
    return                      # sink.feed / overload cancel には絶対入らない
sink = self._engine if self._engine is not None else self._sink
...                            # 以降は従来どおり
```

### 10.6 [MED#3] backpressure
- §10.3 の bounded queue＋chunk drop で対応。drop は throttled warn（毎回ログしない）。

### 10.7 空ストリーム(§14.1)時の挙動
- audio_begin が出ない完了（`_tap_open=False`）では `audio_*` を出さず、**消費側は `done` を終端とする**（PROTOCOL/USECASES に明記）。busy 解放は既存の AudioEnd 冪等解放で満たす。

### 10.8 テスト（敵対系を追加・[LOW]反映）
- capture: engine.begin/feed 不発・`on_audio` が begin→chunk→end 順・chunk が復号 pcm。
- **AudioBegin 後に cancel / server 切断 / ErrorMsg / decode error でも `audio_end`(tap end) が1回出る**。
- `audio_end` が `done` より先に届く（単一 queue で順序保証）。
- 旧接続に残った音声が**新接続へ漏れない**（conn タグ）。
- capture=False で従来再生・`on_audio` 未発火・**直後の GUI speak が無音化しない**（_capture_id 分離）。
- bridge: FakeWS の `start_text_stream(...,capture=False)`/`speak(...,capture=False)` 署名更新、stream/fallback 両方で capture 伝達。
- config: `forward_audio` ロード。

### 10.9 委託順（§9 を更新）
1. `ws_client.py`（`_capture_id`/`_tap_open`/`_is_capture`・AudioBegin/_on_binary/AudioEnd/_finish_stream 分岐・fmt None guard・speak/start_text_stream capture＋effective）＋ `test_ws_textstream.py`
2. `text_stream_bridge.py`（capture 伝達）＋ `test_text_stream_bridge.py`（FakeWS capture kwarg）
3. `text_input.py`（単一 outbound queue＋conn タグ＋bounded＋send_audio/notify/accepted 統一）＋ `test_local_input_server.py`
4. `config.py`（forward_audio）＋ `test_client_config.py`
5. `app.py`（on_audio 配線・bridge capture）
6. docs（USECASES ③・PROTOCOL「クライアント経由 audio out」）
