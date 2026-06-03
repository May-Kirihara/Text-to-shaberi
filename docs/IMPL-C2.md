# C2 実装計画書 v2: クライアント マイク + VAD + バックプレッシャ

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2) §4。対になるサーバは S2(`docs/IMPL-S2.md`/`8c5bb6e..e653922`)で実装済み（`stt_begin`→binary(PCM)→`stt_end`→`transcript`、`auto_tts`なら `audio_begin{parent_id}`/binary/`audio_end` 連鎖、`cancel`→`stopped`、busy=side-band error）。
> 既存クライアントは C1(`a33044a`) で `speak`(text)→再生 が疎通済み（`WSClient`/`PlaybackSink`/`StreamingPlayer`）。
> フロー: 本書 → codexレビュー(`docs/REVIEW-codex-C2.md`) → **v2改訂** → GLM委託 → 私が検証/統合 → `docs/CODE-REVIEW-C2.md`。**この仕様の通りに実装すること。**
> **v2: codexレビュー(`docs/REVIEW-codex-C2.md`)反映** = ①STT送信は **channels=1 固定**（MicCapture 出力は常に int16 mono）・②`utter` の `stt_begin`〜`stt_end` を **単一ロックで atomic 送出**（cancel の割り込み禁止）・③`MicCapture.stop()` は **abort→join→close 順**＋GUI終了は `to_thread`・④`VadSegmenter` の sample_rate/frame_ms を **検証(ValueError)**・⑤`max_speech` 強制確定後も **連続発話を次セグメントへ継承**・⑥terminal error は `_active_id`/`_playback_id` を **区別ログ**・⑦busy-skip ログを **レート制限(≤1/s)**・⑧`auto_tts` 非対応サーバは **認識のみへ降格**＋警告・⑨`error(id=U)` capture-reset テスト追加・⑩🎤に accessibleName/tooltip・⑪`raw.get("mic") or {}` 防御・⑫「<10ms」表現を緩和。
> 前提: S2完了（`e653922`）。`text-to-shaberi-server`(:8765, owned STT) が `ready.features.auto_tts=True` を返す。新規依存は追加しない（`webrtcvad>=2.0.10` は client extra に既存）。

## 1. 目的・スコープ
**マイクに話す → 無音検出で発話を区切る → サーバSTT → `transcript` → 自動TTS → 再生** という音声入力からの縦の疎通を完成させる（READMEのコア機能「常時マイク＋ノイズフィルタ＋無音検出」）。

- スコープ内:
  - `client/audio/vad.py`（**新規** `VadSegmenter`）: webrtcvad による発話セグメント化（純ロジック・I/O無し・webrtcvad は遅延import/注入可）。ノイズゲート（発話判定の床）込み。
  - `client/audio/mic.py`（**新規** `MicCapture`）: `sounddevice.InputStream` をワーカースレッドで**ブロッキング read**しながら `VadSegmenter` に流し、**完成した発話セグメント**を `loop.call_soon_threadsafe` で main へ通知。sounddevice 遅延import・stream/segmenter factory 注入。
  - `client/ws_client.py` 拡張: **STT送信パス** `utter(pcm, auto_tts)`（`stt_begin`→binary×N→`stt_end`）＋ **auto_tts 連鎖対応のディスパッチ**（`transcript`/`audio_begin{parent_id}`/`audio_end`/`stopped`/`error` を発話単位で処理）＋ 送信直列化（`asyncio.Lock`）。
  - `client/config.py` / `client.toml` 拡張: `mic.vad`(aggressiveness/frame_ms/min_speech_ms/max_speech_ms 追加) と `mic.preprocess`(gate_threshold 追加)。
  - `client/ui/main_window.py`: **マイクON/OFFトグル「🎤」** ＋ transcript 表示（ログ流用）。
  - `client/app.py` 配線: トグルで `MicCapture` 起動/停止、セグメント→`ws.utter`、busy中の**ハーフデュプレックス抑止**、transcript 表示。
  - ユニットテスト3系統（VadSegmenter / MicCapture / WSClient の STT・auto_tts）。
- スコープ外:
  - 部分/逐次 transcript（`final=false`）。Qwen は発話単位（full-utterance）なので C2 は **発話確定後に一括送信**（§3.8 の根拠）。
  - スペクトル雑音抑制（`denoise=rnnoise/noisereduce`）・Silero VAD（config 値のみ・import しない）。
  - Opus・wss/TLS（X1）。AEC/バージイン/エコー対策・再接続バックオフ・デバイスホットスワップ（X2）。
  - 入力デバイス選択UI（X2。C2 は `cfg.mic.device` を使用、combo は出力のみ）。

## 2. 既存資産（C1/S2成果）
- `client/ws_client.py`: `WSClient`（hello/ready、`speak`/`cancel`、`audio_begin`/binary/`audio_end`/`stopped`/`error` ディスパッチ、`_active_id` 単一アクティブ、`default_profile`、token urllib エンコード、send失敗ロールバック）。**本書で STT パスと auto_tts 連鎖を追加**。
- `client/audio/sink.py`: `PlaybackSink`（非ブロッキング feed/end/abort、daemon worker、有界 queue、worker ログは `loop.call_soon_threadsafe`）。**再利用**（auto_tts 音声も同じ経路で鳴らす）。
- `client/audio/player.py`: `StreamingPlayer`（遅延スタート・`request_abort`/`close` 分離・factory 注入）。**不変**。
- `client/audio/devices.py`: `list_input_devices()`・`resolve_device(name|index|'default', kind="input")`。**入力解決に流用**。
- `common/protocol.py`: `SttBegin(id, format, auto_tts=True)`・`SttEnd(id)`・`Transcript(id, text, final)`・`AudioBegin(id, parent_id, format)`・`AudioEnd`・`Cancel`・`Stopped`・`ErrorMsg`・`AudioFormatPayload`。**不変**（送受信で使用）。
- `client/config.py`: `MicConfig`(device/sample_rate=16000/channels=1/preprocess/vad)・`VADConfig`(engine/min_silence_ms=500/speech_pad_ms=150)・`MicPreprocessConfig`(gate=True/denoise="off")。**本書でフィールド追加**。
- サーバ S2 ワイヤ（対向・`server/orchestrator.py`）:
  - `stt_begin(id=U)`→（バイナリ蓄積）→`stt_end(id=U)` で `_process_stt` 起動。`transcript(id=U, final=True)` 送出。
  - `auto_tts and text.strip()` のとき **`audio_begin(id="tts-U", parent_id=U)`→binary→`audio_end(id="tts-U")`**。空文字/whitespace なら音声は**来ない**（transcript で終端）。
  - cancel: 取り込み中は `Cancel(id=U)`→`stopped(id=U)`。推論中は `Cancel(id=U)`→`stopped(id=U)`。auto_tts 中は **`Cancel(id=U)` が `active_parent_id` に一致** → `stopped(id="tts-U")`。
  - auto_tts のTTS失敗は **`error(id="tts-U")`**、auto_tts プロファイルロード失敗は `error(id=U)`。
  - 取り込み上限 `_STT_MAX_SECONDS=60`（超過で `error(id=U, "stt audio too large")`＋取り込みリセット）。
- 依存（pyproject `client` extra に既存）: `websockets`・`qasync`・`PySide6`・`sounddevice`・`soxr`・`numpy`・**`webrtcvad>=2.0.10`**。**新規依存は追加しない。**

## 3. 設計

### 3.1 スレッド/イベントループ・モデル（最重要）
qasync で **Qt と asyncio が同一スレッド・同一ループ**（`run_gui` 構築済み）。
- **入力取り込み**は専用 **ワーカースレッド**（`MicCapture._run`）が `sd.InputStream.read()` を**ブロッキング**で回す（イベントループを絶対にブロックしない）。VADセグメント化もこのワーカー内で行う（webrtcvad は C 実装で軽量）。
- ワーカー → main の橋渡しは **`loop.call_soon_threadsafe`** のみ（セグメント通知・ログ）。worker から Qt/ws を直接触らない。
- セグメント通知は main(loop) スレッドの `on_segment(seg)` で受け、`asyncio.ensure_future(ws.utter(seg, ...))` で送信コルーチンを現在ループに載せる。
- **再生は C1 の経路を再利用**（`PlaybackSink` worker スレッド）。

### 3.2 `client/audio/vad.py`: `VadSegmenter`（新規・純ロジック）
webrtcvad の `vad_collector` 系を**発話セグメント完成形で返す**設計。**ストリーミングはしない**（§3.8）。

```python
class VadSegmenter:
    def __init__(self, *, sample_rate=16000, frame_ms=30, aggressiveness=2,
                 min_silence_ms=500, speech_pad_ms=150,
                 min_speech_ms=200, max_speech_ms=30000,
                 gate=True, gate_threshold=200,
                 vad_factory=None):
        ...
    def feed(self, pcm: bytes) -> list[bytes]:   # 完成した発話セグメント(int16 mono bytes)を0件以上返す
    def flush(self) -> bytes | None:             # 停止時、発話中かつ十分長ければ確定して返す
    def reset(self) -> None:
```
- **前提フォーマット**: int16 LE / **mono 固定** / `sample_rate`（webrtcvad は 8000/16000/32000/48000、frame_ms は 10/20/30 のみ）。`frame_bytes = sample_rate*frame_ms/1000*2`。
- **パラメタ検証（codex#4）**: `__init__` で `sample_rate in {8000,16000,32000,48000}`・`frame_ms in {10,20,30}` を検証し、不一致は **`ValueError`**（メッセージに許容集合を明記）。`feed` の入力は **mono int16 前提**（多ch化は `MicCapture` 側で済ませる契約・§3.3）。これにより `client.toml` の誤設定（44100/25ms 等）が実行時の webrtcvad クラッシュではなく**ロード/生成時の明示エラー**になる。
- **webrtcvad は遅延/注入**: `vad_factory` 未指定なら `import webrtcvad; webrtcvad.Vad(aggressiveness)`。指定時は `vad_factory(aggressiveness)`（fake 注入でテスト）。`is_speech(frame, sample_rate)` を呼ぶ。
- **ノイズゲート（発話判定の床）**: サンプルは**改変しない**。`speech = vad.is_speech(frame, sr) and (not gate or _rms_int16(frame) >= gate_threshold)`。`_rms_int16` は `numpy.frombuffer(frame,"<i2")` の二乗平均平方根。定常ノイズでの誤トリガを抑える。`gate_threshold` は int16 RMS（既定200≒-44dBFS、`mic.preprocess.gate_threshold`）。
- **バイト残差バッファ**: `feed` は任意長 bytes を受け、`frame_bytes` 単位に切って処理、端数は次回へ繰越（`_residual`）。
- **状態機械**（フレーム単位）:
  - 派生定数: `pad_frames=ceil(speech_pad_ms/frame_ms)`、`silence_frames=ceil(min_silence_ms/frame_ms)`、`onset_frames=3`（≒frame_ms×3 連続）、`min_speech_frames=ceil(min_speech_ms/frame_ms)`、`max_frames=ceil(max_speech_ms/frame_ms)`。
  - **preroll deque**（idle 中の生フレームを `pad_frames` 分だけ保持）。
  - **idle**: preroll に push。`is_speech` なら `voiced_run+=1` else `0`。`voiced_run>=onset_frames` で **trigger**: `seg=bytearray()` に preroll の全フレームを連結（先頭パッド＋オンセット）→ speaking へ。`silence_run=0`、`speech_frames=preroll内voiced数`。
  - **speaking**: `seg += frame`、`speech_frames` を更新（voiced 加算）。`is_speech` なら `silence_run=0` else `silence_run+=1`。
    - `silence_run>=silence_frames` で **end**: 末尾無音を `pad_frames` 残して切り詰め（任意・MVPは切らず全保持でも可だが pad 超過分はトリム推奨）。`speech_frames>=min_speech_frames` なら確定 `seg` を返却、未満は破棄。idle へ（preroll/各runリセット）。
    - セグメント長が `max_frames` に達したら **強制確定**（話しっぱなし対策・§3.8）→ 確定 `seg` を返却。**ただし発話は継続中なので idle に落とさず speaking を維持**し、新 `seg` を**直前の `pad_frames` 分のフレームで seed**（連続発話の境界を取りこぼさない・codex#5）。`silence_run=0`、`speech_frames` は seed 内 voiced 数で再初期化。`is_speech` が False に転じて初めて通常の end 判定に入る。
  - `feed` は1回の呼び出しで複数セグメント完成し得る（長いブロック投入時）→ list で返す。
- **flush()**: speaking 中で `speech_frames>=min_speech_frames` なら現 `seg` を確定返却、未満は None。reset。
- **依存**: `numpy`（base 依存・top-level可）と遅延 `webrtcvad` のみ。**sounddevice/soxr/torch 等は import しない**。

### 3.3 `client/audio/mic.py`: `MicCapture`（新規）
`sounddevice.InputStream` をワーカースレッドでブロッキング read し、`VadSegmenter` に流して**完成セグメントを通知**。

```python
class MicCapture:
    def __init__(self, *, sample_rate=16000, channels=1, device=None,
                 frame_ms=30, vad_config=None, on_segment=None, on_log=None,
                 loop=None, stream_factory=None, segmenter_factory=None): ...
    def start(self) -> None:   # InputStream生成・start・worker起動。二重startは無視
    def stop(self) -> None:    # stopフラグ→worker join(timeout)→stream stop/close。冪等
    @property
    def active(self) -> bool: ...
```
- **遅延/注入**: `stream_factory` 未指定なら `import sounddevice as sd; sd.InputStream(samplerate, channels, dtype="int16", device, blocksize=frame_samples)`。指定時は同 kwargs で呼ぶ（fake が検証可能）。`segmenter_factory` 未指定なら `VadSegmenter(**vad設定)`。
- **入力デバイス**: `device` は `resolve_device(cfg.mic.device, kind="input")` で解決済み index（None=既定）を受ける。
- **`start()`**: `self._stream = stream_factory(...)`; `self._stream.start()`; `self._stop=False`; `threading.Thread(target=self._run, daemon=True)` を start。
- **`_run()`**（ワーカー）:
  ```
  frame_samples = sample_rate*frame_ms/1000
  while not self._stop:
      data, overflowed = self._stream.read(frame_samples)   # ブロッキング
      if overflowed: self._log("入力オーバーフロー")          # バックプレッシャ可視化
      pcm = _to_int16_mono_bytes(data, channels)            # (frames,ch) np.int16 → mono int16 LE bytes
      for seg in self._segmenter.feed(pcm):
          self._emit_segment(seg)
  tail = self._segmenter.flush()
  if tail: self._emit_segment(tail)
  ```
  - `_to_int16_mono_bytes`: **出力は常に mono int16 LE bytes（契約・codex#1）**。多chは平均で mono 化（int16 維持）。`data.tobytes()`（C連続前提、`np.ascontiguousarray`）。VadSegmenter/STT送信はこの mono 契約に依存する。
  - read 例外（停止時のデバイスclose/abort等）は break して終了（ログのみ）。
- **`_emit_segment(seg)`**: `if self._loop: self._loop.call_soon_threadsafe(self._on_segment, seg)`（main へ）。
- **`_log(msg)`**: `loop.call_soon_threadsafe(self._on_log, msg)`（worker から直接 Qt を触らない）。
- **`stop()`（順序が重要・codex#3）**: `self._stop=True` → **まず `try: self._stream.abort()`（or `stop()`）で blocking `read` を起こす** → `worker.join(timeout=2.0)` → 最後に `try: self._stream.close()`（各 guard）。冪等（二重 stop は無視）。abort で read を即座に解除するため join は実質待たない（最悪でも read 1フレーム ≤30ms）。`stream.abort()`/`stop()` は別スレッド(main)から呼んで安全（PortAudio 仕様）。
  - **GUI終了時のブロック回避**: loop 終了フックからは `await asyncio.to_thread(mic.stop)` で呼び、万一 close が固まってもメインループを塞がない。ユーザのトグルOFF（loop スレッド）では同期 `stop()` でよい（abort 先行で短時間）。
- **境界**: `sounddevice` は遅延import（`--check`/CI で portaudio 不在でも落とさない）。server/torch/qwen/silero は import しない。

### 3.4 `client/ws_client.py`: STT送信 ＋ auto_tts 連鎖（拡張）
**新規状態**: `_playback_id: str|None`（binary を sink に流す対象id）、`_stt_auto_tts: bool`、`_send_lock: asyncio.Lock`。

#### 送信直列化＋STTシーケンスの atomic 化（新規・重要・codex#2）
- `self._send_lock = asyncio.Lock()` を `__init__` に追加。
- **非ロック版の生送信ヘルパ**（ロック内から呼ぶ。再入防止）: `async def _ws_send(self, payload): await self._ws.send(payload)`。`payload` は `model.model_dump_json()`（str）または `bytes`。
- 既存 `_send(model)` を `async with self._send_lock: await self._ws_send(model.model_dump_json())` に変更（cancel/speak/overload-cancel など**単発**制御フレーム用。挙動は C1 と不変）。
- **STT送信は `stt_begin`〜`stt_end` を1回のロック取得で atomic に送る**:
  ```python
  async with self._send_lock:
      await self._ws_send(SttBegin(...).model_dump_json())
      for i in range(0, len(pcm), chunk_bytes):
          await self._ws_send(pcm[i:i+chunk_bytes])
      await self._ws_send(SttEnd(id=id).model_dump_json())
  ```
- 理由: 1 send ごとのロックだと `utter` の binary 連送の**途中に `cancel()`（別コルーチン）が `Cancel` を割り込ませ**、S2 は capture 中 cancel で `_reset_stt_capture()` するため、残り binary/`stt_end` が `error(no active stt stream)` を誘発しライフサイクルが乱れる。シーケンス全体を1ロックで囲むと、`cancel()` の `Cancel` は **`stt_end` 送出後にしか割り込めず**、S2 は「STT推論中 cancel」（`active_stream_id=stt_id`）として正しく `stopped(U)` を返す。`asyncio.Lock` は非再入なので**ロック内では必ず `_ws_send`（非ロック版）を使う**（`_send`/`_send_bytes` を呼ぶと自己デッドロック）。
- なお `cancel()` は **ロック取得前に**「即 `_sink.abort()`＋`_cancelling=True`」を行う（snappy。upload 中は sink 未生成なので abort 対象なし）。`Cancel` 送信は `_send` 経由でロック待ち（upload 完了後）。

#### `utter`（新規・STT発話送信）
```python
async def utter(self, pcm: bytes, auto_tts: bool = True, chunk_bytes: int = 32768) -> None:
```
- 未接続なら `await self.connect()`（失敗で `on_log` し return）。
- **auto_tts feature 降格（codex#8）**: `eff_auto = auto_tts and bool(self._ready and self._ready.features.get("auto_tts"))`。`auto_tts=True` 要求かつサーバ非対応なら **`eff_auto=False`（認識のみ）** に降格し1回だけ警告ログ。以降の `_stt_auto_tts` は `eff_auto` を使う。
- busy（`_active_id is not None`）なら `on_log("発話中のためスキップ")` し return（**ハーフデュプレックス**のローカルガード。app 側でも事前 skip）。
- `id=uuid4().hex`; `_active_id=id`; `_playback_id=None`; `_stt_auto_tts=eff_auto`; `_cancelling=False`; `_first_chunk_logged=False`; `_t_send=loop.time()`; `on_state("busy")`。
- **送信（atomic・失敗で巻き戻し）**: 上記「STTシーケンスの atomic 化」のブロックを `try/except` で囲み、`SttBegin` の format は **常に `channels=1` 固定**（codex#1）:
  ```python
  fmt = AudioFormatPayload(codec="pcm_s16le", sample_rate=self._stt_format.sample_rate, channels=1)
  try:
      async with self._send_lock:
          await self._ws_send(SttBegin(id=id, format=fmt, auto_tts=eff_auto).model_dump_json())
          for i in range(0, len(pcm), chunk_bytes):
              await self._ws_send(pcm[i:i+chunk_bytes])
          await self._ws_send(SttEnd(id=id).model_dump_json())
  except Exception as exc:
      self._finish_stream(); self._state("error", str(exc)); return
  ```
- **channels=1 契約（codex#1）**: `MicCapture` は常に mono int16 を吐く（§3.3）。`cfg.mic.channels` が 2 でも **STT送信は channels=1**（サーバ `_pcm_to_float32(buf, codec, channels)` が mono bytes を多ch解釈して波形破壊するのを防ぐ）。`sample_rate` は `self._stt_format.sample_rate`（既定16000）。
- 送信後は active のまま保持（`transcript`/`audio_begin`/`audio_end`/`error`/`stopped` の受信でライフサイクルを進める）。
- `__init__` に `stt_format: AudioFormatPayload | None = None` を追加（app が `cfg.mic` から `sample_rate` を取って渡す。未指定は 16000/1/pcm_s16le 既定。**channels は送信時に常に1へ上書き**するので stt_format の channels は参照しない）。

#### ディスパッチ拡張（`_on_message` / `_on_binary`）
発話 = **stt_begin →(transcript)→ auto_tts audio_begin/binary/audio_end** を1ライフサイクルで扱う。`speak`(text) の既存挙動（`audio_begin.id==active`）も包含する。
- **`audio_begin`**: 受理条件を **`not _cancelling and (msg.id==_active_id or msg.parent_id==_active_id)`** に拡張。受理時 `_playback_id=msg.id`、`fmt=msg.format or _ready.tts`、`device=_resolve_device()`、`_sink=sink_factory(...)`、`_sink.begin(fmt, device)`。非受理は side-band ログ。
- **binary**: `_active_id and _sink and _playback_id and not _cancelling` のとき `feed`。満杯（overload）→ 既存どおり `_sink.abort()`＋`Cancel(id=_active_id)`＋`_cancelling=True`＋ログ。初回 feed で **TTFA ログ**（`(loop.time()-_t_send)*1000` ms、once。auto_tts 時は「発話送信→初音」）。
- **`transcript`**: `msg.id==_active_id` のとき:
  - `on_log(f"🎤→ {text}")` ＋ `on_state("transcript", text)`（UI表示）。
  - `_cancelling` なら `_finish_stream()`。
  - 否なら **`_stt_auto_tts and text.strip()`** の判定: 真→**音声待ち**（active 保持）、偽→`_finish_stream()`（音声は来ない＝idle化）。**サーバの `auto_tts and text.strip()` と完全一致**させる。
- **`audio_end`**: `msg.id==_playback_id` のとき: `_cancelling` なら `end()` せず `_finish_stream()`、否なら `_sink.end()`→`_finish_stream()`（総量/長さログ）。
- **`stopped`**: `msg.id in {_active_id, _playback_id}` で `_finish_stream()`・ログ"stopped"。
- **`error`**: `msg.id in {_active_id, _playback_id}` を **terminal** とする（auto_tts の `error(id="tts-U")` を取りこぼさない）: `_cancelling` なら `_finish_stream()`、否なら `_sink` あれば `abort()`→`_finish_stream()`→`on_state("error", msg.message)`。それ以外の id/None は **side-band**（ログのみ・継続）。
  - **id 区別ログ（codex#6）**: terminal 時、`msg.id==_playback_id`（=`tts-U`）なら「TTS失敗」、`msg.id==_active_id`（=`U`）なら「STT/プロファイル失敗」と**区別してログ**（`error(id=U)` は STT 推論失敗・auto_tts プロファイルロード失敗の両方を含む。区別表示で原因切り分けを容易に）。
- **`_finish_stream()`**: 既存に加え `_playback_id=None`、`_stt_auto_tts=False` もリセット。

#### `cancel`（拡張）
- `_active_id` 在中なら **即** `_sink.abort()`（あれば）・`_cancelling=True`・`try: await self._send(Cancel(id=self._active_id)) except: pass`。
- **id は常に `_active_id`（=U）を使う**。取り込み中/推論中は `active_stt_id`/`active_stream_id` に、auto_tts 中は **`active_parent_id`** にサーバ側で一致する（S2 の cancel 三者照合）。`tts-U` を送ってはいけない。
- 終端は `stopped(U)` または `stopped(tts-U)` または `error` の受信（上記ディスパッチが両 id を active 帰属として処理）。

#### `is_busy`（新規・補助）
- `def is_busy(self) -> bool: return self._active_id is not None`（app のハーフデュプレックス判定用）。

### 3.5 `client/config.py` / `client.toml`: 設定拡張
- `VADConfig` に追加（既定）: `aggressiveness: int = 2`、`frame_ms: int = 30`、`min_speech_ms: int = 200`、`max_speech_ms: int = 30000`。既存 `engine/min_silence_ms/speech_pad_ms` は不変。
- `MicPreprocessConfig` に追加: `gate_threshold: int = 200`。既存 `gate/denoise` 不変。
- `load_client_config` は `_only(...)` 経由なので**フィールド追加だけで反映**（vad/preprocess とも `_only` でフィルタ済み）。
- **dict 防御（codex#11）**: C2 で `mic` 周りを触るので、`mic_raw = raw.get("mic") or {}` を一度取り、`vad`/`preprocess` も `mic_raw.get("vad") or {}` のように **None/非dict をフォールバック**してから `_only` に渡す（誤設定で読みにくい例外を出さない）。
- `client.toml` の `[mic.vad]`/`[mic.preprocess]` にコメント付きで追記（既定値同値）。`run_check` の mic 表示はそのまま（必要なら aggressiveness 追記）。

### 3.6 `client/ui/main_window.py`: マイクトグル＋transcript表示
- input_row（または device_row）に **「🎤」`QPushButton`（`checkable=True`）** を追加。`set_on_mic_toggle(cb)` を追加し `toggled(bool)` で `cb(checked)` を呼ぶ。`set_mic_active(bool)`: ボタンの checked/ラベルを反映（ON時「🎤 ON」緑系、OFF時「🎤」）。
- **アクセシビリティ/フォント差対策（codex#10）**: ボタンに `setAccessibleName("マイク")` と `setToolTip("マイク常時録音 ON/OFF")` を付与。`🎤` グリフが出ない環境向けにラベル定数を1箇所に持ち（崩れ時 `"Mic"` に差し替え可能）にしておく。
- `set_mic_enabled(bool)`: トグル自体の活殺（auto_tts 非対応サーバ等で無効化・§3.7/codex#8）。
- transcript は `log()` に流す（`🎤→ {text}`）。専用ペインは X2。
- 既存 `set_busy`（text 入力/送信/停止の活殺）は不変。マイクトグルは busy 中も操作可（停止用）。

### 3.7 `client/app.py`: 配線
- WSClient 生成時に `stt_format` を渡す: `AudioFormatPayload(codec="pcm_s16le", sample_rate=cfg.mic.sample_rate, channels=cfg.mic.channels)`（protocol を app で import 済みでなければ追加）。
- `_on_state` に `"transcript"` 分岐追加: `window.log(...)` は WSClient 側で出すなら state では `pass`/任意。重複させない（**transcript 表示は WSClient の `on_log` 経由に一本化**し、state は UIフック用の予約に留める）。
- **マイク配線**:
  ```python
  mic: MicCapture | None = None
  def on_mic_toggle(active: bool):
      nonlocal mic
      if active:
          if mic is None:
              from .audio.mic import MicCapture
              from .audio.devices import resolve_device
              dev = resolve_device(cfg.mic.device, kind="input")
              mic = MicCapture(sample_rate=cfg.mic.sample_rate, channels=cfg.mic.channels,
                               device=dev, frame_ms=cfg.mic.vad.frame_ms,
                               vad_config=cfg.mic, on_segment=on_segment,
                               on_log=window.log, loop=loop)
          try: mic.start(); window.log("[mic] 録音開始")
          except Exception as exc:
              window.log(f"[mic] 開始失敗: {exc}"); window.set_mic_active(False)
      else:
          if mic is not None: mic.stop(); window.log("[mic] 録音停止")
  _last_skip_log = [0.0]
  def on_segment(seg: bytes):
      if ws.is_busy():
          now = loop.time()                                   # busy中スキップ＝ハーフデュプレックス
          if now - _last_skip_log[0] >= 1.0:                  # ログをレート制限(≤1/s・codex#7)
              window.log("[mic] 発話中につきスキップ"); _last_skip_log[0] = now
          return
      asyncio.ensure_future(ws.utter(seg, auto_tts=True))
  window.set_on_mic_toggle(on_mic_toggle)
  ```
  - `on_segment` は **main(loop) スレッド**で実行（`MicCapture` が `call_soon_threadsafe` 経由）。`ws.is_busy()` で busy 中の新規発話を捨てる（フィードバックループ抑止）。**自分の TTS 再生を VAD が拾い続けてもログが溢れない**ようスキップログは**1秒に1回**へ抑制（codex#7）。`utter` 内の auto_tts 降格（§3.4/codex#8）で `auto_tts=True` を渡しても安全。
  - **auto_tts 非対応サーバ（codex#8）**: `_on_state("ready")` 受信時に `payload.features.get("auto_tts")` を確認し、false なら `window.log("[mic] サーバが auto_tts 非対応: 認識のみ")` ＋ **マイクトグルを無効化**（`window.set_mic_enabled(False)`）。対応サーバでは有効化。
  - `MicCapture` への `vad_config` は `cfg.mic`（segmenter_factory 既定が `cfg.mic.vad`/`cfg.mic.preprocess` から `VadSegmenter` を組む。frame_ms/sample_rate 検証は §3.2 で `ValueError`）。
  - 終了時（loop 終了フック）に **`await asyncio.to_thread(mic.stop)`** を best-effort（メインループを塞がない・codex#3）。トグルOFF（ユーザ操作）は同期 `mic.stop()`（abort 先行で短時間）。
- `run_check` に mic device 解決の試行を追加してよい（任意）。GUI 非起動時は MicCapture を生成しない。

### 3.8 ハーフデュプレックス／バックプレッシャ方針（設計判断）
- **発話確定後に一括送信する根拠**: サーバの Qwen STT は**発話単位（full-utterance）**で、`stt_end` 受信まで推論を開始しない（`_process_stt` は全バッファに対して実行）。よって発話中に binary を逐次ストリームしても**STTレイテンシは改善しない**。クライアントで発話完成まで貯めてから一括送信する方が、(a) `min_speech_ms` 未満の blip を**送る前に**棄却でき、(b) 状態機械が単純（mid-stream cancel 不要）になる。**一括送信の転送時間は STT/TTS 推論時間に比べて十分小さく無視できる**（数秒の16k mono 音声＝数百KB・codex#12）。
- **バックプレッシャ（最小）**:
  1. **入力オーバーフロー**: `InputStream.read` の `overflowed` を検知しログ（ワーカーが詰まると PortAudio がドロップ）。
  2. **発話長上限**: `max_speech_ms`（既定30s）で強制確定。話し続けても無限バッファしない。サーバ側 `_STT_MAX_SECONDS=60` の手前で確定。
  3. **ハーフデュプレックス**: busy（再生/送信中）に来たセグメントは app で**破棄**＋ログ（フィードバック・多重発話の抑止）。
  4. **再生過負荷**: C1 既存（`sink.feed` False → abort + cancel）を流用。
- **送信直列化**: `_send_lock` で全 ws.send を直列化（§3.4）。

### 3.9 フォーマット
- マイク取込・STT送信は **`pcm_s16le` / `cfg.mic.sample_rate`(16000) / `cfg.mic.channels`(1)** 固定。webrtcvad 制約（8/16/32/48k・10/20/30ms）と整合。
- 再生（auto_tts 音声）は `audio_begin.format`（GPT-SoVITS 既定 32000/mono）→ `StreamingPlayer` 内 soxr で出力デバイス rate へ。C1 と同一。

## 4. 受け入れ基準
- [ ] **VadSegmenter ユニット**（webrtcvad/sounddevice 不要・fake VAD 注入）: 無音→発話→無音 の合成 int16 で **1セグメント**を返し、長さ≈ preroll+発話(+末尾pad) であること。`min_speech_ms` 未満の短バーストは**破棄**。`max_speech_ms` で強制確定。**強制確定後も連続発話が次セグメントに継承される**（max 分割の境界で取りこぼさない・codex#5）。`flush()` が発話中の残りを確定。`gate=True` で `gate_threshold` 未満 RMS のフレームは speech 判定されない。`feed` の端数バイトが次回に繰越される。**不正 `sample_rate`/`frame_ms` で `ValueError`**（codex#4）。
- [ ] **MicCapture ユニット**（sounddevice 不要・fake stream/segmenter 注入）: fake stream の `read()` が scripted ブロック→停止を返すと、`segmenter.feed` の戻りが `on_segment` に **`loop.call_soon_threadsafe` 経由**で渡る。`overflowed=True` でログ。`stop()` が worker を join し冪等。flush セグメントも通知。
- [ ] **WSClient STT/auto_tts ユニット**（fake WS サーバ・`asyncio.run`）:
  - `utter(pcm, auto_tts=True)` が `stt_begin`→binary×N→`stt_end` を送る（受領 fake サーバで検証）。
  - サーバが `transcript(id=U)`＋`audio_begin(id="tts-U", parent_id=U)`＋binary＋`audio_end("tts-U")` を返すと、**fake sink** が `begin/feed×N/end` を受け、`on_state("transcript", text)` が発火、最終 `idle`。
  - **空 transcript**（`text=""`）では音声を待たず `idle` 化（auto_tts 不発火と一致）。
  - **auto_tts の terminal error**（`error(id="tts-U")`）で `sink.abort`＋`idle`＋`on_state("error")`。**`error(id=U)`（STT/プロファイル失敗）も terminal で idle 化**し、id 区別ログが出る（codex#6）。
  - **server capture-reset**（codex#9）: `stt_begin`/binary 後にサーバが **`error(id=U, "stt audio too large")`** を返すと client が `idle` 化し、**続けて次の `utter` を送れる**（active が残らない）。
  - **channels=1 固定（codex#1）**: 受領 `SttBegin.format.channels==1`（`stt_format`/cfg が 2 でも 1 で送る）。
  - **auto_tts 降格（codex#8）**: `ready.features.auto_tts` 無しのサーバへ `utter(auto_tts=True)` すると **`SttBegin.auto_tts==False`** で送られ、警告ログが出る。
  - `cancel()` が **`Cancel(id=U)`** を送り（`tts-U` でない）、sink 即 abort、`stopped(id="tts-U")`/`stopped(id=U)` のいずれでも `idle` 終端。
  - `utter` 送信失敗で active/busy を残さない（巻き戻し）。
  - **送信 atomic（codex#2）**: `utter` の `stt_begin`〜`stt_end` 送出中に `cancel()` が割り込めず（`Cancel` は `stt_end` 後に送られる）、binary が分断されない。
- [ ] **C1 リグレッション**: 既存 `test_ws_client.py`（speak/side-band/terminal/cancel/token/default_profile/rollback）が全 green（`_playback_id` 一般化後も `audio_begin.id==active` 経路は不変）。
- [ ] **境界/コンパイル**: AST 境界（client→server/torch/qwen/silero 禁止。`webrtcvad` は遅延import/注入で**トップレベル import しない**、`sounddevice` も遅延）・py_compile・既存pytest 全 green。
- [ ] **統合（手動・私がLinuxで）**: `text-to-shaberi-server`(owned STT)＋実 GPT-SoVITS 起動 → GUI で「🎤」ON → マイクに発話 → 無音で区切られ **`🎤→ <認識文>`** がログ表示 → **auto_tts 音声が再生**（先頭欠け無し）。再度発話で繰り返し。busy 中の発話がスキップされる。発話→初音の TTFA ログ。
- [ ] **Windows実機（あなた）**: WASAPI 入力（既定マイク）で取り込み、無音区切り・認識・再生が動く。マイクON/OFFトグル。

## 5. 変更/新規ファイル
- `client/audio/vad.py`（**新規**: `VadSegmenter`）
- `client/audio/mic.py`（**新規**: `MicCapture`）
- `client/ws_client.py`（`utter`/`is_busy`/`_send_bytes`/`_send_lock`・auto_tts 連鎖ディスパッチ・`_playback_id`/`_stt_auto_tts`/`stt_format`）
- `client/config.py`（`VADConfig`+aggressiveness/frame_ms/min_speech_ms/max_speech_ms、`MicPreprocessConfig`+gate_threshold）
- `client.toml`（`[mic.vad]`/`[mic.preprocess]` 追記）
- `client/ui/main_window.py`（🎤 トグル・`set_on_mic_toggle`/`set_mic_active`・transcript ログ）
- `client/app.py`（MicCapture 配線・on_mic_toggle/on_segment・ハーフデュプレックス・stt_format）
- `tests/test_vad_segmenter.py`（**新規**）
- `tests/test_mic_capture.py`（**新規**）
- `tests/test_ws_client.py`（**STT/auto_tts ケース追加**）

## 6. テスト/検証
- **ユニット（必須・GPT-SoVITS不要・sounddevice/webrtcvad不要）**:
  - `test_vad_segmenter.py`: `vad_factory` に scripted fake（フレーム index 列で True/False を返す）を注入。合成 int16（`numpy` でゼロ＝無音、一定振幅＝発話）を `feed`。セグメント数・長さ・min/max・**max分割の連続性（codex#5）**・gate・flush・端数繰越を assert。**不正 `sample_rate`/`frame_ms` で `ValueError`（codex#4）**。
  - `test_mic_capture.py`: fake stream（`read()` が `(np.ndarray, overflowed)` を scripted に返し、尽きたら例外/停止。`abort`/`stop`/`close` 呼び出しを記録）＋ fake segmenter（feed の戻りを scripted）。`asyncio.new_event_loop()` を用意し `call_soon_threadsafe` 到達で `on_segment`/`on_log` を確認。`stop()` が **abort→join→close 順**（codex#3）・冪等。多ch 入力が **mono bytes 化**される（codex#1）。
  - `test_ws_client.py`（追加）: `websockets.serve` で S2 シーケンス（transcript＋auto_tts、空transcript、`error(id=U)`/`error(id="tts-U")` の両 terminal、**`error(id=U,"stt audio too large")` 後に次 utter 可（codex#9）**、cancel→stopped(tts-U)/stopped(U)）を返す fake サーバ。`FakeSink`/`FakeSinkFactory`（既存）流用で begin/feed/end/abort を検証。`utter` 送信内容（**SttBegin.format.channels==1**・binary・SttEnd）を受領側で確認。**auto_tts 非対応 ready で `SttBegin.auto_tts==False` 降格（codex#8）**。**send atomic（codex#2）**: `cancel` を upload 中に呼んでも `Cancel` が `stt_end` 後に届く。
- **統合（手動）**: §4。サーバ＋実 GPT-SoVITS＋実マイク/オーディオ。Linux は私、Windows はあなた。
- AST境界・py_compile・既存pytest 全 green 維持。**新規依存ゼロ**。

## 7. リスク・留意点
- **発話単位送信の妥当性（§3.8）**: Qwen は full-utterance。逐次ストリームしても STT は早くならない。確定後一括送信が単純かつ等価。逐次 transcript（`final=false`）は将来（バックエンド差替時）。
- **auto_tts 連鎖の id 規則**: 受信音声は `id="tts-U"`/`parent_id=U`。クライアントは **`parent_id==_active_id`** で受理し `_playback_id=msg.id` を別管理。`audio_end`/`error`/`stopped` は `_playback_id`（または `_active_id`）帰属で終端。cancel は**常に `Cancel(id=_active_id=U)`**（サーバが `active_parent_id` で一致）。`tts-U` を cancel に使わない。
- **空 transcript の終端**: サーバは `text.strip()` 空なら auto_tts を出さない。クライアントも **同条件**で `transcript` 受信時に idle 化しないと、来ない音声を待って固まる。
- **ハーフデュプレックス**: busy 中のセグメントは app で破棄。これが無いと TTS 出力→スピーカ→マイク→再 STT のループや多重発話。AEC/バージインは X2。ヘッドフォン運用（ボイチェン/AITuber）では実害小。
- **スレッド安全**: 取り込み worker は `sd.InputStream.read()` をブロッキング。Qt/ws へは **`call_soon_threadsafe`** のみ。`stop()` の `join` は read 1フレーム(≤30ms)で抜けるため短時間。worker から直接 Qt を触らない。
- **送信直列化（新規 `_send_lock`）**: `utter` の binary 連送中に `cancel`/overload-cancel が `ws.send` を並行呼び出しすると websockets が壊れる。全送信を1ロックで直列化。既存 `_send` も lock 化（C1 挙動は不変）。
- **webrtcvad 制約**: フレームは 10/20/30ms・8/16/32/48k・int16 mono のみ。`frame_ms`/`sample_rate` をこの集合に限定（既定 30ms/16k）。`is_speech` には**正確に `frame_bytes` 長**を渡す（segmenter が保証）。
- **ノイズゲートはサンプル非改変**: 判定の床（RMS閾値）として作用。実スペクトル抑制（rnnoise/noisereduce）は `denoise` で将来。`gate_threshold` は環境依存のため要調整（適応ノイズフロアは X2）。
- **境界/遅延import**: `webrtcvad`・`sounddevice` はトップレベル import 禁止（factory 内/関数内）。`--check`/CI（portaudio/webrtcvad 不在でも）で落とさない。`numpy` は base 依存で top-level 可。
- **新規依存ゼロ**: `webrtcvad` は client extra 既存。`silero`・`rnnoise`・`noisereduce` 等は**追加しない**。
- **C1 リグレッション防止**: `_on_message` の `audio_begin` 受理条件・binary feed・`audio_end` を `_playback_id` 一般化する際、`speak`(text) の `id==active` 経路を壊さない（既存 `test_ws_client.py` を維持）。
- **channels=1 契約（codex#1）**: `MicCapture` は常に mono int16 を吐き、`utter` の `SttBegin.format.channels` は **常に 1**。`cfg.mic.channels=2` でもサーバへ mono として送る（多ch解釈による波形破壊を防止）。
- **STT送信の atomic 化（codex#2）**: `stt_begin`〜`stt_end` は1ロック取得で送り切る。`asyncio.Lock` は非再入のため**ロック内は `_ws_send`（非ロック版）**を使う（`_send`/`_send_bytes` を呼ぶと自己デッドロック）。`cancel` の `Cancel` はシーケンス後にしか割り込めず、S2 が「STT推論中 cancel」として `stopped(U)` を返す。
- **MicCapture 停止順序（codex#3）**: `stop flag → stream.abort()/stop()（read を起こす）→ join → close`。GUI終了は `await asyncio.to_thread(mic.stop)`。逆順だと Qt メインスレッドを最大 timeout 分塞ぎ、worker 生存のまま close する競合になる。
- **auto_tts 非対応サーバ（codex#8）**: `ready.features.auto_tts` が無い旧サーバでは `utter` が `auto_tts=False` に降格（認識のみ）し、app はマイクトグルを無効化＋警告。無言で固まらせない。
- **busy-skip ログ抑制（codex#7）**: 常時録音が自分の TTS をVADで拾い続けてもログが溢れないよう、スキップログは ≤1/s に抑制。根本的な mic pause / AEC は X2。
