# IMPL-TEXTSTREAM — AITuber向け「LLMテキスト→順次TTS」低遅延パイプライン

> 目的: LLM等が生成するテキストを**文単位でストリーム入力**し、speak_stream の連続音声生成基盤を**STTを抜いて再利用**することで、**gapless・低遅延**にライブ配信へ流す。出力は既存 StreamingPlayer → 仮想デバイス → OBS をそのまま使う。
>
> 確定方針（2026-06-04, May）: ①合成は `StreamSession` を **text-segment モード**に拡張（one-shot は1セグメントのストリームとして同経路）。②文分割は**クライアント側** `TextSegmenter`。③入力トランスポートは**ローカルWebSocket**（既存 `websockets` 流用・依存ゼロ）。

関連: [[speak-stream-feature-status]] / `docs/IMPL-SPEAKSTREAM-PHASEA.md`(StreamSession土台) / `docs/STREAMING.md`(マイク版ユーザーガイド)

---

## 0. 背景と非目標

- **背景**: speak_stream は本質的に「順序付き・gapless・先行パイプラインTTS」。マイク版は `音声→STT→テキスト断片→順序TTS→連続音声`。AITuber版は **STT段が無いだけ**で同型 = `LLMテキスト断片→順序TTS→連続音声`。
- **非目標(v1)**: LLMの呼び出し自体（外部オーケストレータの責務）。複数TTS N≥2 の並列生成（Phase C で別途。ただし text モードもその恩恵を自動で受ける拡張点に乗る）。`prev_tail` 文脈。Windows 実機検証（後続）。
- **排他**: text-stream はマイクの audio-stream と同じ `active_stream_session` スロットを使う ⇒ **同時併用不可**（mic録音中はLLM入力をbusyで弾く。仕様として明記）。

---

## 1. 全体アーキ

```
┌ LLMオーケストレータ ┐  ws(127.0.0.1)   ┌──────── GUIクライアント ────────┐  ws(既存)   ┌ サーバ(GPU) ┐
│ 生テキストを逐次送信 │ ──{text}/final─► │ LocalInputServer                │            │ StreamSession│
└─────────────────────┘  ◄─accepted/done─ │  → TextSegmenter(句点で分割)     │ ─stream_   │  (text_mode) │
                                          │  → TextStreamBridge(直列/idle)  │  begin     │  _stt_worker │
                                          │  → ws.start_text_stream/...     │  seg_text  │   をスキップ │
                                          │ StreamingPlayer ◄── 連続音声 ────┼──audio─────┤  _gen_output │
                                          └──┬──────────────────────────────┘            └──────────────┘
                                             ▼ 仮想オーディオ(TTS_Virtual) → OBS capture
```

**支配項は TTS 先頭チャンク(TTFA)**。入力側で効く低遅延レバーは「全文を待たず**最初の文で生成開始**」の一点。連続文の継ぎ目は reorder＋連続音声で gapless。

---

## 2. プロトコル拡張（`common/protocol.py`）

後方互換のため `StreamBegin` にモードを追加し、テキスト用セグメントメッセージを新設。音声用 `SegBegin/SegEnd`＋binary はそのまま。

```python
class StreamBegin(BaseModel):
    type: Literal["stream_begin"] = "stream_begin"
    id: str = ""
    format: AudioFormatPayload = AudioFormatPayload()
    auto_tts: bool = True
    mode: Literal["audio", "text"] = "audio"   # ★追加。既定 audio で既存挙動不変

class SegText(BaseModel):                       # ★新設（text モード専用）
    type: Literal["seg_text"] = "seg_text"
    id: str = ""
    index: int = 0
    text: str = ""

# _MESSAGE_TYPES に "seg_text": SegText を登録
```

- サーバ `ready.features` に **`"text_stream": True`** を追加（クライアントの能力ゲート用。speak_stream と同様）。

---

## 3. サーバ `StreamSession` の text-mode 拡張（`server/orchestrator.py`）

`_text_q` は既に `(index, text)` と `_SEG_END` を受けて連続音声を生成する。**text モードは入口を変えるだけ**。

```python
# StreamSession.__init__: 引数追加
def __init__(self, session, stream_id, fmt, auto_tts, profile, text_mode: bool = False):
    ...
    self.text_mode = text_mode

# start(): text モードは STT ワーカを起動しない
def start(self) -> None:
    if self.text_mode:
        self._tasks = [asyncio.create_task(self._gen_output_worker())]
    else:
        self._tasks = [
            asyncio.create_task(self._stt_worker()),
            asyncio.create_task(self._gen_output_worker()),
        ]

# 新メソッド: テキストを直接 _text_q へ（auto_tts/空文字はここで判定）
def feed_text(self, index: int, text: str) -> None:
    if self.auto_tts and text.strip():
        self._text_q.put_nowait((index, text))

# mark_end(): モードで投入先を変える
def mark_end(self) -> None:
    if self.text_mode:
        self._text_q.put_nowait(_SEG_END)
    else:
        self._seg_in_q.put_nowait(_SEG_END)
```

ハンドラ追加と `_handle_stream_begin` 分岐:

```python
# _handle_stream_begin: mode を渡す
sess = StreamSession(self, msg.id, msg.format, msg.auto_tts, profile,
                     text_mode=(msg.mode == "text"))

# 新ハンドラ
async def _handle_seg_text(self, msg: SegText) -> None:
    s = self.active_stream_session
    if s is None or msg.id != s.stream_id or not s.text_mode:
        await self._send_model(ErrorMsg(id=msg.id, message="no active text stream"))
        return
    s.feed_text(msg.index, msg.text)

# ディスパッチに elif isinstance(parsed, SegText): await self._handle_seg_text(parsed)
# features に "text_stream": True を追加
```

- **順序保証**: クライアントは `_send_lock` 下で index 昇順に送り、受信ループは逐次処理 ⇒ `_text_q` は FIFO=index順。N=1ではこれで十分（N≥2拡張時に reorder が効く）。
- **stop/cancel/cleanup**: 既存の `stop()`/`_cleanup()`/`_send_stopped_best_effort_stream` がそのまま効く（`_tasks` をキャンセルするだけ）。変更不要。
- **Transcript エコー**: text モードでは送らない（テキストは送信元が既知。GUI表示はクライアント側でログ）。

---

## 4. クライアント `ws_client.py` text-stream API

audio 版（`start_stream/send_stream_segment/end_stream`）に対応する text 版を追加。受信側（`audio_begin/binary/audio_end`）は audio モードと同一なので**変更不要**。

```python
async def start_text_stream(self, auto_tts: bool = True) -> str | None:
    if not self.is_connected():
        if not await self.connect():
            self._log("接続できません"); return None
    if not bool(self._ready and self._ready.features.get("text_stream")):
        return None
    if self._active_id is not None:
        return None
    stream_id = uuid.uuid4().hex
    self._active_id = stream_id
    self._playback_id = None
    self._stream_mode = True
    eff_auto = auto_tts and bool(self._ready and self._ready.features.get("auto_tts"))
    self._stt_auto_tts = eff_auto
    self._cancelling = False
    self._first_chunk_logged = False
    self._t_send = (self._loop or asyncio.get_event_loop()).time()
    self._state("busy")
    try:
        await self._send(StreamBegin(id=stream_id, mode="text", auto_tts=eff_auto))
    except Exception as exc:
        self._finish_stream(); self._state("error", str(exc))
        if not self.is_connected(): self._schedule_reconnect()
        return None
    return stream_id

async def send_text_segment(self, index: int, text: str) -> None:
    if self._active_id is None or not self._stream_mode:
        return
    await self._send(SegText(id=self._active_id, index=index, text=text))

async def end_text_stream(self) -> None:
    if self._active_id is None or not self._stream_mode:
        return
    await self._send(StreamEnd(id=self._active_id))
```

- `cancel()` / `is_busy()` / 受信処理は既存を流用。

---

## 5. クライアント `TextSegmenter`（`client/text_input.py`・純ロジック）

マイク版 `StreamingVadSegmenter` のテキスト対応物。**I/O なし・テスト容易**。イベントは vad.py の `StreamStart/StreamComplete` を再利用し、`TextSegment(index, text)` を新設（vad.py の `SubSegment` に対応、音声ではなくテキスト）。

```python
@dataclass(slots=True)
class TextSegment:
    index: int
    text: str

class TextSegmenter:
    def __init__(self, *, boundary_chars="。．.!?！？\n", max_chars=80, hard_max_chars=160):
        ...
        self.reset()

    def reset(self):
        self._buf = ""; self._index = 0; self._active = False

    def feed(self, text: str) -> list:
        events = []
        if not self._active and text:
            events.append(StreamStart()); self._active = True; self._index = 0
        self._buf += text
        # 句点境界で切る（境界文字を含めて1セグメント）。max_chars 超で最寄り境界 or 強制 hard_max。
        while True:
            cut = self._find_cut()
            if cut is None: break
            seg, self._buf = self._buf[:cut], self._buf[cut:]
            if seg.strip():
                events.append(TextSegment(index=self._index, text=seg)); self._index += 1
        return events

    def flush(self) -> list:
        events = []
        if self._active:
            if self._buf.strip():
                events.append(TextSegment(index=self._index, text=self._buf)); self._index += 1
            events.append(StreamComplete()); self.reset()
        return events

    def _find_cut(self) -> int | None:
        # 1) 先頭から最初の境界文字位置+1 を返す（その境界までで1文）
        # 2) 無ければ len(buf) >= hard_max_chars のとき max_chars 近辺(直近の空白/読点)で強制cut
        ...
```

分割規則（v1）:
- **文境界**: `boundary_chars` のいずれかが出たらそこまでで1セグメント（**テキストは1文字も捨てない**＝min足切りは無し）。
- **長文の強制cut**: 境界が来ないまま `hard_max_chars` を超えたら `max_chars` 近辺の読点/空白で切る（無ければそのまま `max_chars`）。レイテンシ上限の保険。
- 連続呼び出しで**部分トークン**を渡されても安全に蓄積（LLMのトークンストリームをそのまま `feed` 可能）。

---

## 6. クライアント `LocalInputServer`（`client/text_input.py`・WSサーバ）

`websockets.serve`（既存依存・websockets 16系の単一引数ハンドラ `async def handler(conn)` / `conn.request.path` を使用。`ws_client` の `additional_headers` と同世代）。

```python
class LocalInputServer:
    def __init__(self, *, host, port, token, on_text, on_final, on_cancel, on_log, loop):
        # on_text(text:str, profile:str|None), on_final(), on_cancel()
        ...
    async def start(self):
        # セキュリティ: host が loopback 以外 かつ token 空 → RuntimeError で起動拒否（サーバ側 bind ルールと対称）
        self._server = await websockets.serve(self._handle, self._host, self._port)
    async def _handle(self, conn):
        if not self._authorized(conn):   # ?token= or Authorization: Bearer
            await conn.close(code=4401, reason="unauthorized"); return
        self._clients.add(conn)
        try:
            async for raw in conn:
                msg = json.loads(raw)            # {text?, profile?, final?, type?}
                if msg.get("type") == "cancel":
                    self._on_cancel(); continue
                if "text" in msg:
                    self._on_text(msg["text"], msg.get("profile"))
                if msg.get("final") or msg.get("type") == "end":
                    self._on_final()
                await conn.send(json.dumps({"event": "accepted"}))
        finally:
            self._clients.discard(conn)
    async def broadcast(self, event: dict):     # done/error/speaking を全接続へ
        ...
    async def close(self):
        self._server.close(); await self._server.wait_closed()
```

**ローカルWS 公開契約（外部連携者向け・USECASES.md にも転記）**:
- 接続: `ws://127.0.0.1:<port>`（token設定時は `?token=<t>` か `Authorization: Bearer <t>`）。
- 送信(JSON): `{"text":"…"}` 追記（増分OK）/ `{"text":"…","final":true}` 追記して確定 / `{"final":true}` 確定のみ / `{"type":"cancel"}` 中断。`profile` はストリーム先頭のみ尊重。接続切断＝確定扱い。
- 受信(JSON): `{"event":"accepted"}` 受領 / `{"event":"done"}` 音声ストリーム完了（ペーシング用）/ `{"event":"error","message":…}`。

---

## 7. クライアント `TextStreamBridge`（`client/text_stream_bridge.py`）

`StreamBridge`（音声）と同じ状態機械を踏襲（入力側/wire側分離・`_ws_idle` Event で発話間直列化・Queue＋単一consumer・queue overflowはoldest drop）。差分は**呼ぶwsメソッドが text 版**で、fallback が `speak`(one-shot)。

```python
class TextStreamBridge:
    # StreamStart → (idle待ち) start_text_stream;  None→discard
    # TextSegment → send_text_segment(index,text);  unsupported時は _fallback_buf へ連結
    # StreamComplete → end_text_stream;  unsupported時は ws.speak("".join(fallback))
    # set_supported / notify_ws_busy / notify_ws_idle / start / close(_STOP sentinel) は StreamBridge と同形
    # on_done コールバック: StreamComplete を wire に流し終え idle 復帰時に呼ぶ → LocalInputServer.broadcast({"event":"done"})
```

> 設計判断: テスト済みの `StreamBridge` を壊さないため**姉妹クラスとして新設**（音声/テキストで型が違う＝SubSegment vs TextSegment、start_stream vs start_text_stream）。共通化は codex の判断を仰ぐ（過度な抽象化は避けたい）。

---

## 8. 設定 `InputConfig`（`client/config.py`）

```python
@dataclass(slots=True)
class InputConfig:
    enabled: bool = False          # 既定オフ（待受ソケットを開くので opt-in）
    host: str = "127.0.0.1"
    port: int = 8770               # server:8765 / gpt-sovits:9880 と衝突しない既定
    token: str = ""                # 任意の共有秘密。空＝loopbackのみ前提で認証なし
    seg_max_chars: int = 80
    seg_hard_max_chars: int = 160

# ClientConfig に input: InputConfig 追加。load_client_config で [input] を読む。
# clamp/security: host が loopback 以外 かつ token 空 → 警告し enabled=False に落とす（fail-safe）。
#                 seg_hard_max_chars = max(seg_hard_max_chars, seg_max_chars)。
```

---

## 9. `app.py` 配線

- `cfg.input.enabled` のとき `LocalInputServer` + `TextStreamBridge` を構築。
- `_on_state` ready: `feats.get("text_stream")` を `text_supported[0]` に。bridge.set_supported。未対応なら log（fallback=one-shot speak で動作継続）。
- LocalInputServer のコールバック配線:
  - `on_text(text, profile)` → `segmenter.feed(text)` の各イベントを `bridge.handle_event` へ（profileは先頭セグメントへ反映する経路を用意）。
  - `on_final()` → `segmenter.flush()` の各イベントを `bridge.handle_event` へ。
  - `on_cancel()` → `asyncio.ensure_future(ws.cancel())` ＋ segmenter.reset。
- bridge の `on_done` → `asyncio.ensure_future(local_server.broadcast({"event":"done"}))`。
- `_startup()`: `cfg.input.enabled` なら `await local_server.start()`（失敗は log のみで GUI 継続）。bridge.start()。
- `_shutdown()`: `await local_server.close()` → `await bridge.close()`。
- **排他**: mic が active（`active_mic_mode` 非None）の間に text 入力が来たら、`start_text_stream` が busy で None を返す→bridge が discard（＝LLM側に `error` を返してもよい）。

---

## 10. テスト計画（pytest・pytest-asyncio不使用＝`asyncio.run`）

- `test_protocol_textstream.py`: `StreamBegin(mode="text")` round-trip / `SegText` parse / `_MESSAGE_TYPES` 登録。
- `test_text_segmenter.py`: 句点分割 / 増分feedで部分トークン蓄積 / hard_max強制cut / flushで残り＋StreamComplete / 1文字も捨てない。
- `test_text_stream_bridge.py`: 正常順（start_text→send昇順→end）/ unsupported→fallback speak / start None discard / idle gating / close cancel。FakeWS（start_text_stream/send_text_segment/end_text_stream/speak/cancel）。
- `test_ws_text_stream.py`: FakeWS送信列が StreamBegin(text)→SegText…→StreamEnd / text_stream未対応で start_text_stream→None。
- `test_local_input_server.py`: 実 `websockets` で 127.0.0.1 に接続→`{text}`/`{final}`→on_text/on_final 発火、token不一致で4401、`done` broadcast。
- `test_client_config.py` 追記: `[input]` ロード / loopback外+token空で enabled=False に落ちる。
- サーバ統合（任意・既存 test_ws_speak_stream に倣う）: text_mode StreamSession が STT を呼ばず `_text_q`→TTS で audio_begin/end 各1回。

---

## 11. 受け入れ条件

1. `cfg.input.enabled=true` 起動で `ws://127.0.0.1:8770` が待受。外部から `{text}`×複数＋`{final}` を送ると、**1本の連続音声**（audio_begin/end 各1回）が再生される。
2. 文をまたいで**gapless**（seg境界でブツ切れない）。最初の文の句点到達で**先行生成が始まる**（全文完成を待たない）。
3. one-shot（`{"text":"全文","final":true}` 1発）も同経路で再生（1セグメントのストリーム）。
4. サーバが `text_stream` 未対応なら **one-shot speak にfallback**して動作継続（トグル/起動は壊れない）。
5. `host` が loopback 以外 かつ token 空なら**起動拒否（enabled=Falseへ）**。token不一致接続は 4401。
6. mic録音中は text 入力が busy で弾かれる（併用不可の仕様どおり）。
7. pytest 全緑＋ruff クリーン。

---

## 12. GLM委託の割り（ファイル単位・小委託厳守）

教訓（[[glm-delegation-uv-lock-contention]]）に従い**1ファイル=1委託**、「extended reasoningなしで今すぐ編集」＋自己検証コマンド付き。順序:

1. `common/protocol.py`（StreamBegin.mode / SegText / 登録）＋ `test_protocol_textstream.py`
2. `server/orchestrator.py`（text_mode 分岐・feed_text・_handle_seg_text・features・dispatch）
3. `client/ws_client.py`（start_text_stream/send_text_segment/end_text_stream・SegText import）＋ `test_ws_text_stream.py`
4. `client/text_input.py` の `TextSegmenter`（純ロジック）＋ `test_text_segmenter.py`
5. `client/text_input.py` の `LocalInputServer`（websockets）＋ `test_local_input_server.py`
6. `client/text_stream_bridge.py`（`TextStreamBridge`）＋ `test_text_stream_bridge.py`
7. `client/config.py`（InputConfig＋clamp/security）＋ `test_client_config.py` 追記
8. `client/app.py`（配線・startup/shutdown・ready gating・fallback）

各ステップ後に `uv run pytest -q <該当> && uv run ruff check <該当>` で自己検証。

---

## 13. ドキュメント連携

実装・検証後、`docs/USECASES.md`（新規）にユースケース別ガイドを書く:
- **TTSフロントエンド**（GUIテキスト欄）/ **ボイスチェンジャー**（mic→STT→TTS→仮想シンク）/ **AITuber**（本機能：LLM→ローカルWS→順次TTS→OBS。§6の公開契約と最小サンプル送信スクリプトを掲載）。
- README に導線（STREAMING.md と同様）。

---

## 14. codexレビュー反映（v2・確定仕様。本節が §2〜§13 を上書きする）

codex(gpt-5.5) 総評: **条件付きGO**。設計方向は既存構造に合致。以下を反映して v1 実装へ進む。

### 14.1 [HIGH#1/#2] 音声ゼロでも正常終了通知＝busy stuck 解消（＋既存 audio-mode の潜在バグも同時修正）
- **問題**: text/`auto_tts=False`/全空白だと `_text_q` に `_SEG_END` のみ → `_gen_output_worker` は `_audio_begun=False` のまま正常終了し **AudioBegin/AudioEnd を一切送らない** → クライアントは AudioEnd/StreamStopped/Error が来ず `_active_id` を解放できず **busy stuck**。これは **audio speak_stream でも「全seg の STT が空」のとき起きる既存の潜在バグ**。
- **確定修正（orchestrator `_gen_output_worker`）**: 正常終端（`_SEG_END` 到達 & `not _cancelled`）では `_audio_begun` の真偽に関わらず **必ず `AudioEnd(id=stream_id)` を送る**（音声があった場合は従来どおり、無かった場合も AudioEnd だけ送る）。
- **クライアント（ws_client `_on_message` の AudioEnd 処理）**: AudioEnd 受信で **AudioBegin/playback の有無に関わらず `_finish_stream()` で `_active_id` を冪等に解放**することを確認・保証（未確認なら修正）。
- **テスト**: 「空テキストのみの text stream → AudioEnd が来て client idle 復帰」/ audio-mode「全seg空 STT → 同様に idle」。

### 14.2 [HIGH#3] `StreamBegin.profile` を追加（per-stream プロファイル）
- `protocol.py`: `StreamBegin.profile: str | None = None`。
- `orchestrator._handle_stream_begin`: `load_profile(..., msg.profile or cfg.tts.active_profile)`（speak と同じ流儀）。audio speak_stream にも恩恵。
- `ws_client`: `start_stream(profile=None)` / `start_text_stream(profile=None)` 追加→ `StreamBegin.profile` に載せる。fallback `speak(text, profile)` も同じ profile を使う。
- `app/bridge`: profile は **ストリーム先頭** で `start_text_stream(profile=...)` に渡す。LocalInputServer は最初の `{profile}` を保持。

### 14.3 [HIGH#4/#5] LocalInputServer＝単一アクティブ接続＋相関、mic 排他は app 入口で明示
- **単一アクティブ入力接続(v1)**: 最初に接続した1本のみ受理。進行中の追加接続は `{"event":"error","message":"busy"}` を返して即 close。`accepted/done/error` は**そのアクティブ接続のみ**へ送る（broadcast 廃止＝相関確保）。複数接続多重化は将来。
- **接続切断=確定** は「アクティブ接続のみ」。非アクティブ接続の切断は無視。
- **mic 排他は app 入口で明示拒否**: `on_text/on_final` の入口で `active_mic_mode[0] is not None` なら text 入力を受けず Local WS へ `{"event":"error","message":"busy: mic active"}`。`start_text_stream()->None` 任せにしない（mic有効でも未発話なら `_active_id is None` で誤って開始してしまうため）。

### 14.4 [MED#1] ws_client 送信の `stream_id` 検証（二重防御）
- `send_text_segment`/`end_text_stream`（および既存 `send_stream_segment`/`end_stream`）は **ロック内で `sid=self._active_id` を確認**し、None/別idなら no-op。bridge 単一 consumer が主経路だが await 跨ぎの取りこぼしを防ぐ。
- テスト: concurrent send/end/cancel。

### 14.5 [MED#2] TextStreamBridge は control event を捨てない
- overflow 時 `StreamStart`/`StreamComplete` は **絶対に捨てない**。`TextSegment` が `queue_max` を超えたら、その発話を `failed` にして以後の TextSegment を捨て **Local WS に error を返す**（黙って落とさない＝「1文字も捨てない」要件と整合）。

### 14.6 [MED#3] fallback(one-shot speak) の相関
- unsupported/fallback 経路: bridge は `_ws_idle` を待ってから `ws.speak(text, profile)` を呼ぶ。idle 復帰で Local WS に `done`、エラーで `error`。`speak()` の busy-drop には依存しない。

### 14.7 [MED#4] websockets 16.0 認証（確定コード）
```python
from urllib.parse import parse_qsl, urlsplit
async def _handle(self, conn):
    path = conn.request.path
    qs_token = dict(parse_qsl(urlsplit(path).query)).get("token", "")
    auth = conn.request.headers.get("Authorization", "")
    bearer = auth[7:] if auth.startswith("Bearer ") else ""
    if self._token and self._token not in (qs_token, bearer):
        await conn.close(code=4401, reason="unauthorized"); return
```
旧式 `(websocket, path)` / `request_headers` は使わない（websockets 16 で壊れる）。

### 14.8 [MED#5] TextSegmenter 分割規則の精緻化（日本語）
- 境界文字の直後に連続する **終端記号と閉じ括弧** `」』）)】〕]` を同一セグメントに含める（句点直後の括弧を次segに残さない）。
- `.` は **前後が数字なら境界扱いしない**（`3.14`）。`!?`/`！？` 連続は1境界にまとめる。
- hard cut は 読点/空白 優先、無ければ `max_chars`。**property test で「全入力の連結 == 全出力セグメントの連結」（1文字も増減なし）を保証**。

### 14.9 [LOW] その他
- `app`: `text_supported[0]` を `stream_supported`(mic用) と**別管理**。ready で `feats.get("text_stream")`。
- `ws_client.start_text_stream`: `_metric_kind="text_stream"`、初期 metrics(`stt_ms=None`) 発火（TTFA が "speak" 扱いにならないよう）。
- `LocalInputServer` 堅牢化: `websockets.serve(..., max_size=<上限>)`、text 長 clamp、JSON parse 失敗は `{"event":"error"}` 返し（落とさない）、`_closing` 中は新規接続拒否。

### 14.10 テスト/受け入れ/委託順の更新
- **テスト追加**: 空入力→idle復帰 / 2接続目 busy error / mic active 中の text 拒否 / concurrent send-end-cancel / segmenter property(不増減) / fallback done 相関 / StreamBegin.profile が反映される。
- **受け入れ条件に追加**: 「空入力でも client が idle 復帰」「2接続目は busy error」「mic 録音中は text を明示拒否」「per-stream profile 指定が効く」。
- **委託順は §12 のまま**（protocol→orchestrator→ws_client→TextSegmenter→LocalInputServer→TextStreamBridge→config→app）。各ファイルに本節 v2 を織り込む。
