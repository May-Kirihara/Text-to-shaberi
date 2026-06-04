# IMPL-SPEAKSTREAM Phase A — 実装委託仕様（headless / pytest 検証可能な土台）

> 親計画: `docs/IMPL-SPEAKSTREAM.md`（**§9 が確定仕様**）。本書はその Phase A（§9.8）を**実装可能な粒度**に落とした作業指示。
> 対象基準コミット: `0523af6`。
> **Phase A スコープ = サーバ/プロトコル/VAD ロジック/WSClient 送出API + 単体テスト。GUI 配線（app.py の mic→stream）は Phase B（本タスクに含めない）。**
> 制約: 既存テスト（`uv run pytest tests/ -q`）を**1つも壊さない**。既存の単発 speak/stt 経路（`_busy()` 直列）は**不変**。streaming は**別状態機械**。

---

## 0. 設計の核（§9 要約・ここを外さない）

1. **音声は stream 単位で1本**（§9.1）。サーバは `audio_begin{id=stream}` を**1回**だけ送り、全 seg の PCM/opus を**index 順に連続**で送り、`audio_end{id=stream}` を**1回**だけ送る。**seg 境界を音声フレーミングに出さない**（seg は `transcript{index}` 字幕と内部順序制御のみが持つ）。
2. **STT 先行 → 順序付き連続出力**（§9.2）。STT ワーカは seg k の TTS 出力中に seg k+1 を先行 transcribe（パイプライン）。出力は**厳密に index 昇順**の連続音声。
   - Phase A は **生成 N=1**（単一の gen/output ワーカが index 順に各 seg を tts.stream して1本の連続音声へ流す）。seg k+1 の TTFA は**クライアント側 PlaybackEngine のバッファ余剰**（サーバが実時間より速く送る＝RTF<1 で蓄積）で隠れる。
   - **「複数TTS」= N≥2** は後続。`_gen_output_worker` を **gen プール(N)＋reorder バッファ（next_index だけ emit）** に差し替えるのが拡張点。Phase A では**その差し替え点をコメントで明示**するだけ（実装は N=1）。
3. **StreamSession は別状態機械**（§9.3）。`active_task` とは別に `self.active_stream_session` を持ち、`_busy()` に**算入**。`stop()` は**冪等**（`stream_stopped` を1回だけ）。
4. **STT cancel は executor を止められない**（§9.3）。`cancelled` フラグを持ち、executor 完了後の **Transcript 送信・text enqueue を必ず抑止**。**STT ワーカは1固定**。
5. **ケイパビリティ交渉**（§9.4）。`ready.features["speak_stream"]=True`。未対応サーバには WSClient が `start_stream` で False を返し**呼び側が従来 utter にフォールバック**できるようにする。
6. **StreamingVadSegmenter は二段階無音**（§9.5）。`seg_silence`（短=サブseg確定）と `end_silence`（長=stream終端）を分離。
7. **拡張点を型に用意**（§9.6）: `seg_begin` に `prev_tail: str = ""`（Phase A 未使用、句文脈を後で渡せるように）。

---

## 1. プロトコル拡張 — `src/text_to_shaberi/common/protocol.py`

新メッセージを追加（pydantic v2, 既存と同じ書式）:

```python
class StreamBegin(BaseModel):
    type: Literal["stream_begin"] = "stream_begin"
    id: str = ""                                  # stream id
    format: AudioFormatPayload = AudioFormatPayload()
    auto_tts: bool = True

class SegBegin(BaseModel):
    type: Literal["seg_begin"] = "seg_begin"
    id: str = ""                                  # stream id
    index: int = 0
    format: AudioFormatPayload = AudioFormatPayload()
    prev_tail: str = ""                           # §9.6 拡張点（Phase A 未使用）

class SegEnd(BaseModel):
    type: Literal["seg_end"] = "seg_end"
    id: str = ""
    index: int = 0

class StreamEnd(BaseModel):
    type: Literal["stream_end"] = "stream_end"
    id: str = ""

class StreamStopped(BaseModel):
    type: Literal["stream_stopped"] = "stream_stopped"
    id: str = ""
```

- `Transcript` に **`index: int | None = None`** を追加（既存フィールドは不変）。
- `_MESSAGE_TYPES` に `stream_begin/seg_begin/seg_end/stream_end/stream_stopped` を登録。
- **音声出力は既存の `AudioBegin/AudioEnd` を再利用**（`id=stream_id`, `parent_id=None`）。新しい音声メッセージは作らない。

> 設計理由: クライアントの `_on_message` は `AudioBegin.id == _active_id` で再生開始する。stream では `_active_id = stream_id` なので既存経路がそのまま使える。

---

## 2. サーバ StreamSession — `src/text_to_shaberi/server/orchestrator.py`

### 2.1 Session への最小追加

- `__init__`: `self.active_stream_session: "StreamSession | None" = None` を追加。
- `_busy()` を拡張:
  ```python
  def _busy(self) -> bool:
      return (
          self.active_stt_id is not None
          or (self.active_task is not None and not self.active_task.done())
          or (self.active_stream_session is not None and not self.active_stream_session.done())
      )
  ```
- `_handshake` の `features` を `{"auto_tts": True, "speak_stream": True}` に。
- `_message_loop` に分岐追加（既存分岐の前後どちらでも可、可読性優先）:
  ```python
  elif isinstance(parsed, StreamBegin): await self._handle_stream_begin(parsed)
  elif isinstance(parsed, SegBegin):    await self._handle_seg_begin(parsed)
  elif isinstance(parsed, SegEnd):      await self._handle_seg_end(parsed)
  elif isinstance(parsed, StreamEnd):   await self._handle_stream_end(parsed)
  ```
- **binary routing**: `_handle_binary` 冒頭で stream を優先:
  ```python
  async def _handle_binary(self, data: bytes) -> None:
      if self.active_stream_session is not None:
          self.active_stream_session.feed_binary(data)
          return
      # ↓既存の stt バッファ処理はそのまま
  ```
- **cancel routing**: `_handle_cancel` 先頭に追加:
  ```python
  if self.active_stream_session is not None and msg.id == self.active_stream_session.stream_id:
      await self.active_stream_session.stop()   # 冪等。stream_stopped を1回送る
      return
  # ↓既存の stt/active_task 分岐はそのまま
  ```
- `_cleanup()`: 切断時に `if self.active_stream_session is not None: await self.active_stream_session.stop()` を追加（best-effort）。

### 2.2 ハンドラ

```python
async def _handle_stream_begin(self, msg: StreamBegin) -> None:
    if not msg.id:
        await self._send_model(ErrorMsg(id=None, message="stream_begin.id is required")); return
    if self._busy():
        await self._send_model(ErrorMsg(id=msg.id, message="busy: stream already active")); return
    from .config import ServerConfig
    cfg: ServerConfig = self.state.config
    try:
        profile = load_profile(self.state.profiles_dir, cfg.tts.active_profile)
    except Exception as exc:
        await self._send_model(ErrorMsg(id=msg.id, message=f"failed to load profile: {exc}")); return
    sess = StreamSession(self, msg.id, msg.format, msg.auto_tts, profile)
    self.active_stream_session = sess
    sess.start()

async def _handle_seg_begin(self, msg: SegBegin) -> None:
    s = self.active_stream_session
    if s is None or msg.id != s.stream_id:
        await self._send_model(ErrorMsg(id=msg.id, message="no active stream")); return
    s.open_segment(msg.index)

async def _handle_seg_end(self, msg: SegEnd) -> None:
    s = self.active_stream_session
    if s is None or msg.id != s.stream_id:
        await self._send_model(ErrorMsg(id=msg.id, message="no active stream")); return
    s.close_segment(msg.index)

async def _handle_stream_end(self, msg: StreamEnd) -> None:
    s = self.active_stream_session
    if s is None or msg.id != s.stream_id:
        await self._send_model(ErrorMsg(id=msg.id, message="no active stream")); return
    s.mark_end()
```

### 2.3 StreamSession 本体（同ファイル orchestrator.py 内に新クラス）

**状態機械（3 コルーチン）:**

- 入力: `seg_begin → open_segment(index)`（`_cur_index=index, _cur_buf=bytearray()`）, `feed_binary(data)`（`_cur_buf += data`）, `seg_end → close_segment(index)`（`_seg_in_q.put_nowait((index, bytes(_cur_buf)))`, `_cur_buf=None`）, `stream_end → mark_end()`（`_seg_in_q.put_nowait(_END)`）。
- **`_stt_worker`（1タスク）**: index 順に取り出し transcribe。**順序保証**のため worker は1固定。
- **`_gen_output_worker`（1タスク）**: text を index 順に取り出し、1本の連続音声へ tts.stream を流す。

```python
_SEG_END = object()  # sentinel

class StreamSession:
    def __init__(self, session, stream_id, fmt, auto_tts, profile):
        self.session = session              # 送信は session._send_model / _send_bytes / _send_lock を使う
        self.stream_id = stream_id
        self.fmt = fmt                      # クライアント入力 PCM フォーマット(AudioFormatPayload)
        self.auto_tts = auto_tts
        self.profile = profile
        self._seg_in_q: asyncio.Queue = asyncio.Queue()
        self._text_q: asyncio.Queue = asyncio.Queue()   # (index, text) / _SEG_END  ※STT先行の余裕
        self._cur_index: int | None = None
        self._cur_buf: bytearray | None = None
        self._cancelled = False
        self._stopped_sent = False
        self._audio_begun = False
        self._tasks: list[asyncio.Task] = []

    def start(self):
        self._tasks = [
            asyncio.create_task(self._stt_worker()),
            asyncio.create_task(self._gen_output_worker()),
        ]

    def done(self) -> bool:
        return bool(self._tasks) and all(t.done() for t in self._tasks)

    # --- 入力 ---
    def open_segment(self, index):
        self._cur_index = index; self._cur_buf = bytearray()
    def feed_binary(self, data):
        if self._cur_buf is not None: self._cur_buf += data
    def close_segment(self, index):
        if self._cur_buf is not None:
            self._seg_in_q.put_nowait((index, bytes(self._cur_buf)))
        self._cur_buf = None; self._cur_index = None
    def mark_end(self):
        self._seg_in_q.put_nowait(_SEG_END)

    # --- STT ワーカ（1固定・cancelled フラグで遅延結果破棄）---
    async def _stt_worker(self):
        loop = asyncio.get_running_loop()
        try:
            while True:
                item = await self._seg_in_q.get()
                if item is _SEG_END:
                    await self._text_q.put(_SEG_END); return
                index, buf = item
                fmt = self.fmt
                frame_bytes = bytes_per_sample(fmt.codec) * fmt.channels
                usable = len(buf) - (len(buf) % frame_bytes)
                samples = _pcm_to_float32(buf[:usable], fmt.codec, fmt.channels)
                if samples.size == 0:
                    text = ""
                else:
                    text = await loop.run_in_executor(
                        self.session.state.stt_executor,
                        self.session.state.stt_engine.transcribe,
                        samples, fmt.sample_rate, self.session.state.language,
                    )
                if self._cancelled:          # ★executor 完了後のガード（§9.3）
                    continue
                await self.session._send_model(Transcript(id=self.stream_id, index=index, text=text, final=True))
                if self.auto_tts and text.strip():
                    await self._text_q.put((index, text))
        except asyncio.CancelledError:
            raise

    # --- 生成/出力ワーカ（N=1・1本の連続音声）---
    async def _gen_output_worker(self):
        # ★N≥2 拡張点: ここを「gen プール(N)→reorder(next_index)→output」に差し替える(§9.2)。
        #   Phase A は N=1: text を index 順に取り出し、単一 OpusStreamEncoder を全 seg で共有して連続送出。
        cfg = self.session.state.config
        use_opus = (self.session.codec == OPUS_CODEC_NAME)
        enc = None
        try:
            while True:
                item = await self._text_q.get()
                if item is _SEG_END:
                    break
                index, text = item
                if not self._audio_begun:
                    self._audio_begun = True
                    if use_opus:
                        await self.session._send_model(AudioBegin(id=self.stream_id, parent_id=None, format=opus_wire_format()))
                        enc = OpusStreamEncoder(src_rate=self.profile.audio.sample_rate, bitrate=cfg.transport.opus_bitrate)
                    else:
                        fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=self.profile.audio.sample_rate, channels=self.profile.audio.channels)
                        await self.session._send_model(AudioBegin(id=self.stream_id, parent_id=None, format=fmt))
                src_channels = self.profile.audio.channels
                src_codec = self.profile.audio.encoding or "pcm_s16le"
                async for chunk in self.session.state.tts_client.stream(self.profile, text):
                    if self._cancelled: break
                    if use_opus:
                        mono = self.session._to_mono_int16(chunk, src_codec, src_channels)
                        for pkt in enc.feed(mono):
                            await self.session._send_bytes(pkt)
                    else:
                        await self.session._send_bytes(chunk)
                if self._cancelled: break
            # 正常終端
            if self._audio_begun and not self._cancelled:
                if use_opus and enc is not None:
                    for pkt in enc.flush():
                        await self.session._send_bytes(pkt)
                await self.session._send_model(AudioEnd(id=self.stream_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.session._send_model(ErrorMsg(id=self.stream_id, message=repr(exc)))
        finally:
            # 自然終了時の後始末（cancel 経由でない場合）
            if self.session.active_stream_session is self and not self._cancelled:
                self.session.active_stream_session = None

    # --- 冪等 stop（cancel/切断）---
    async def stop(self):
        if self._stopped_sent:
            return
        self._stopped_sent = True
        self._cancelled = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try: await t
            except (asyncio.CancelledError, Exception): pass
        await self.session._send_stopped_best_effort_stream(self.stream_id)  # StreamStopped を送る(下記)
        if self.session.active_stream_session is self:
            self.session.active_stream_session = None
```

- Session に **`_send_stopped_best_effort_stream`** を追加（`StreamStopped(id=...)` を best-effort 送信。既存 `_send_stopped_best_effort` は `Stopped` 用なので別メソッド）。
- import 追加: `StreamBegin, SegBegin, SegEnd, StreamEnd, StreamStopped`。
- **二重 stopped 回避**（§9.3）: stream 経路は `_stream_tts` を使わないので `Stopped` は出ない。`stop()` の `StreamStopped` のみ。`_gen_output_worker` の CancelledError では `Stopped`/`AudioEnd` を**送らない**（stop() 側が StreamStopped を送る）。

---

## 3. StreamingVadSegmenter — `src/text_to_shaberi/client/audio/vad.py`

既存 `VadSegmenter` は**触らず**、新クラス＋イベント型を追加。

```python
from dataclasses import dataclass

@dataclass(slots=True)
class StreamStart: ...                 # onset（最初の発話検出）→ 呼び側は start_stream()
@dataclass(slots=True)
class SubSegment:                      # 句相当サブセグメント確定 → send_stream_segment(index, audio)
    index: int
    audio: bytes
@dataclass(slots=True)
class StreamComplete: ...              # 発話末（長無音）→ end_stream()
```

`StreamingVadSegmenter`（二段階無音）:
- パラメータ: `sample_rate, frame_ms, aggressiveness, gate, gate_threshold, vad_factory`（既存と同様）＋
  - `seg_silence_ms=250`（短・サブseg確定）, `end_silence_ms=500`（長・stream終端）, `seg_min_ms=400`（極小片は次へ連結＝確定しない）, `seg_max_ms=2500`（ポーズ無しでも切る）, `speech_pad_ms=150`。
  - frame_ms=30 では ms→frame は ceil 丸め（既存 `VadSegmenter` と同じ `math.ceil`）。`seg_silence_frames < end_silence_frames` を assert。
- `feed(pcm: bytes) -> list`（イベントのリスト）, `flush() -> list`。
- **状態**: `idle → speaking → gap`。
  - **idle**: preroll を貯め、voiced が `onset_frames=3` 連続で **speaking** へ遷移し、`StreamStart` を emit、preroll を seg 先頭に投入、`_index=0`。
  - **speaking**: フレームを seg に積む。voiced→`silence_run=0`。silence→`silence_run++`。
    - `silence_run == seg_silence_frames` に達した時点で、その seg の speech フレーム数 `>= seg_min_frames` なら **`SubSegment(_index, seg)` を emit、`_index++`**、seg を**リセット**（pad seed は付けてよい）、状態 **gap** へ。`seg_min` 未満なら確定せず seg を保持したまま gap へ（無音が続けば end 判定、再発話で継続）。
    - seg 長が `seg_max_frames` に達したら **`SubSegment` emit, `_index++`**、seg を pad seed でリセットし **speaking 継続**（gap には行かない）。
  - **gap**: 直近サブseg確定後の無音待ち。voiced フレームで **speaking 復帰**（同一 stream の次サブseg。silence_run=0、preroll seed を seg 先頭へ）。silence が続き `silence_run >= end_silence_frames` で **`StreamComplete` emit、`reset()`（idle へ）**。
- `flush()`: speaking/gap 中なら、保留 seg が `seg_min` 以上あれば `SubSegment` を emit してから `StreamComplete` を emit、`reset()`。idle なら空リスト。

> 実値の丸め（30ms frame で 250→270ms 等）は親計画 §9.5 どおり。テストは frame 数ベースで検証する。

---

## 4. WSClient stream 送出 API — `src/text_to_shaberi/client/ws_client.py`

新メソッド3つ＋ stream 受信処理。**既存 `speak`/`utter` は不変。**

### 4.1 送出 API

```python
async def start_stream(self, auto_tts: bool = True) -> str | None:
    # 接続確認 → features.speak_stream を確認（未対応なら None を返す＝呼び側フォールバック）
    # _active_id 在中なら None（多重不可）
    # stream_id 生成 → _active_id=stream_id, _playback_id=None, _stream_mode=True,
    #   _stt_auto_tts=auto_tts(かつサーバ対応), _cancelling=False, _first_chunk_logged=False
    # _state("busy"); StreamBegin(id, format=_stt_format, auto_tts) を送る
    # 成功で stream_id を返す
async def send_stream_segment(self, index: int, pcm: bytes, chunk_bytes: int = 32768) -> None:
    # _active_id/_stream_mode 前提。_send_lock 内で
    #   SegBegin(id=_active_id, index, format=...) → binary×N → SegEnd(id=_active_id, index)
async def end_stream(self) -> None:
    # _send_lock 内で StreamEnd(id=_active_id) を送る（_active_id は AudioEnd/StreamStopped で片付く）
```

- `__init__` に `self._stream_mode: bool = False` を追加。`_finish_stream()` で `self._stream_mode = False` をリセット。
- `features.speak_stream` 判定は `bool(self._ready and self._ready.features.get("speak_stream"))`。

### 4.2 stream 受信処理（`_on_message` / `_on_binary`）

- **AudioBegin**: 既存ロジックで OK（`msg.id == _active_id` で `engine.begin` 1回 → `_playback_id=stream_id`）。stream でも**変更不要**。
- **binary（`_on_binary`）**: 既存で OK（`_playback_id` セット済みなら `engine.feed`）。**変更不要**。
- **Transcript**: stream_mode を分岐。**stream では字幕のみ・絶対に `_finish_stream()` しない**:
  ```python
  if isinstance(msg, Transcript) and msg.id == self._active_id:
      if self._stream_mode:
          self._state("transcript", msg.text)   # index 付きで UI に出すなら ("transcript", {"index":msg.index,"text":msg.text})
          return                                  # ★finish しない（空 text でも継続）
      # ↓既存の単発ロジック（空transcript/auto_tts判定で finish）はそのまま
  ```
- **AudioEnd**: 既存で OK（`msg.id == _playback_id == stream_id` → `engine.end()` 1回 → `_finish_stream()`）。stream 終端＝音声1本の終わり。**変更不要**。
- **StreamStopped（新）**: `if msg.id == self._active_id: self._log("stream stopped"); self._finish_stream()`。`cancel()` 後の確定。
- `cancel()` は既存のまま（`Cancel(id=_active_id)` を送る＝stream_id）。サーバが `StreamStopped` を返す。

> 重要: stream の transcript は index ごとに何度も来る。既存単発経路は transcript で finish するが、**stream_mode では finish しない**ことが唯一の肝。これを外すと最初の seg で再生が切れる（親計画 §9.1 の現行クライアント破壊と同根）。

---

## 5. テスト（pytest・GPU/マイク不要・fake 注入）

既存の `tests/test_ws_stt.py` の `FakeTTSClient`/`FakeSTTEngine`/`_make_config`/`TestClient` 流儀を踏襲。

### 5.1 `tests/test_ws_speak_stream.py`（新規）
- **順序＆単一音声**: 3 seg を `stream_begin → (seg_begin,bytes,seg_end)×3 → stream_end` で送る。受信を集計し:
  - `audio_begin` が **ちょうど1回**（id==stream_id, parent_id is None）。
  - `audio_end` が **ちょうど1回**（id==stream_id）。
  - `transcript` が **3回**、`index` が 0,1,2 の順。
  - 音声 bytes 合計 > 0。
- **STT 先行（パイプライン）**: `FakeSTTEngine` が各 seg で別テキストを返すよう拡張（index で出し分け）し、transcript の index 昇順＆音声が1本に連続することを確認。
- **cancel→stream_stopped**: seg 投入後（または TTS 進行中）に `cancel(id=stream)` → `stream_stopped`(id==stream) が**1回**。`Stopped`(単発用) は**来ない**。
- **STT cancelled-flag**: `BlockingFakeSTTEngine`（既存）で transcribe を block 中に `cancel` → unblock しても **その seg の transcript が送られない**（遅延結果破棄, §9.3）。`stream_stopped` が来る。
- **冪等 stop**: `cancel` を2回送っても `stream_stopped` は1回だけ（2回目は no-op か error、二重 stopped は不可）。
- **busy 相互排他**: stream 中に `speak`/`stt_begin` は `busy` error。stream 中に2つ目の `stream_begin` も `busy` error。
- **features**: `ready.features["speak_stream"] is True`。
- **RTF/アンダーラン seam**: `FakeTTSClient` に**チャンク毎 `asyncio.sleep(delay)` を注入**できる引数を足し（生成遅延の擬似）、`delay` を変えても **順序が壊れない**・`audio_begin`/`audio_end` が各1回であることを確認（真のアンダーランは実機計測だが、パイプラインが詰まらず順序保証されることを seam で担保）。

### 5.2 `tests/test_streaming_vad.py`（新規）
- `FakeVAD`（`tests/test_vad_segmenter.py` の流儀, `is_speech` を decisions で制御）注入。
- onset で `StreamStart` 1回。
- 「発話→短無音(seg_silence)→発話→長無音(end_silence)」で `SubSegment(index=0)`, `SubSegment(index=1)`, `StreamComplete` の順。
- `seg_min` 未満の極小発話は確定されず次へ連結される。
- `seg_max` 到達で speaking 継続のまま分割（gap に行かない）。
- `flush()` が保留 seg を吐いてから `StreamComplete`。

### 5.3 `tests/test_protocol_stream.py`（新規 or 既存 protocol テストに追記）
- 新メッセージ5種が `parse_message` で往復できる。`Transcript(index=2)` が往復で `index==2`。

---

## 6. 受け入れ条件（このタスクの完了定義）

1. `uv run pytest tests/ -q` が **全 pass**（既存テスト不変・新テスト追加）。
2. 既存 `test_ws_stt.py` / `test_ws_tts.py` / `test_vad_segmenter.py` が**1つも壊れない**。
3. `audio_begin`/`audio_end` が stream で**各1回**・`transcript.index` が昇順・`cancel→stream_stopped`(1回)・STT cancelled-flag で遅延結果破棄、を 5.1 のテストで実証。
4. `ruff`（あれば）/ import 境界テスト `test_import_boundaries.py` を壊さない（client は server を import しない 等）。
5. GUI（app.py）配線は**やらない**。`StreamingVadSegmenter` と WSClient stream API は**単体テストで検証**できる状態。

---

## 7. やらないこと（Phase A 範囲外）

- app.py の mic→stream 配線・設定UI（⚙）への seg_silence 等の露出（Phase B）。
- 生成 N≥2（複数 GPT-SoVITS / reorder バッファ並列化）= 後続（`_gen_output_worker` の差し替え点コメントのみ残す）。
- seg 句文脈プロンプト（`prev_tail` の実利用）= Phase C。
- 真の再デコード型ストリーミングSTT。

---

## 8. 注意（落とし穴）

- **送信は必ず `session._send_model`/`_send_bytes` 経由**（`_send_lock` 直列化。生 `ws.send` 禁止＝フレーム交錯）。
- `_pcm_to_float32` / `bytes_per_sample` は `..common.pcm` から（orchestrator 既存 import）。
- opus 連続化: `OpusStreamEncoder` は **stream 全体で1個**を共有し最後に1回 `flush()`。seg ごとに作り直さない。
- `deploy/server.docker.toml` などの秘密ファイルには触らない。
- 既存 `_busy()` のテスト（`test_stt_begin_while_capture_is_busy` 等）が緑のままであること（stream 追加で legacy busy 判定を変えない）。
