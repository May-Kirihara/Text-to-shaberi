# S2 実装計画書 v2: サーバ STT over WebSocket（+ auto_tts）

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2) §4。S1(`docs/IMPL-S1.md`)の Session 状態機械に **STT 経路**を足す。
> フロー: 本書 → codexレビュー → GLM委託 → 検証。**この仕様の通りに実装すること。**
> **v2: codexレビュー(`docs/REVIEW-codex-S2.md`)反映** = ①GPU直列化は asyncio.Lock ではなく**単一ワーカー executor**（to_thread のキャンセルが推論を止めない問題）・②`_stream_tts(parent_id, manage_active)` 正式化（auto_tts は manage_active=False）・③cancel時 `stopped.id=active_stream_id` を固定しC2は parent_id 照合・④PCM端数byteを full-frame に切詰め・⑤stt_end の id 不一致は side-band で capture 継続・⑥サイズ上限は秒数ベース(`_STT_MAX_SECONDS`)・⑦owned起動は backend=="qwen_local" のみ・⑧STT推論中 cancel を受け入れ基準に追加・⑨`_reset_stt_capture()` helper・⑩既存 `tests/test_ws_tts.py` の ready 期待値(auto_tts)更新。
> 前提: S1完了（`88877ed`）, C1完了（`a33044a`）。GPT-SoVITS api_v2 :9880 稼働。Qwen3-ASR は `qwen-asr` で利用可能（server extra に既存）。

## 1. 目的・スコープ
クライアントの **音声フレーム(stt_begin→binary→stt_end) → Qwen3-ASR で文字起こし → `transcript` 返却**。`auto_tts` 指定時は **同一セッション内で TTS に連結**（`audio_begin{id="tts-<sttid>", parent_id="<sttid>"}…audio_end`）。`stt_begin` 重複は拒否。

- スコープ内:
  - `stt_begin`/binary/`stt_end` の状態機械、`transcript` 送信。
  - `auto_tts=true` のサーバ内 STT→TTS 連結（`parent_id`）。
  - `stt_begin` 中の新規 `stt_begin`/`speak` 拒否（busy=side-band error）。
  - STT エンジン抽象 `STTEngine` ＋ `QwenSTTEngine` 実装 ＋ **注入/lifespan 所有権**（S1 の TTSClient と同型）。
  - **STT 推論はワーカースレッド**（`asyncio.to_thread`）＋ **GPU直列化（共有 asyncio.Lock）**。
  - `ready.features.auto_tts = true`、`AudioBegin.parent_id` 送出。
  - 言語マッピング固定（config `ja` ⇔ Qwen `"Japanese"`）。
  - サーバ単体検証（FakeSTTEngine）＋ 任意の実機統合。
- スコープ外:
  - クライアントのマイク/VAD/送信（C2）。部分/逐次 transcript（MVPは発話単位・`final=true` のみ）。Opus（X1）。クライアント往復TTS（auto_tts=false 時の C 側TTSは C2/後続）。

## 2. 既存資産（S1/C1成果）
- `server/orchestrator.py: Session`：`_send_model`/`_send_bytes`/`_send_stopped_best_effort`、`_receive_message`（**現状 binary→`error("binary unsupported in S1")`**→本書で変更）、`_handshake`（**`features.auto_tts: False`**→true化）、`_handle_speak`/`_stream_tts`（**`AudioBegin` に parent_id 追加**）/`_handle_cancel`、`_cleanup`、`active_stream_id`/`active_task`/`_send_lock`。
- `server/config.py`：`STTConfig`(backend/language/qwen_local)・`QwenLocalConfig`(model/device/dtype/attn_implementation)。
- `server/tts/`：`TTSClient.stream`・`load_profile`・`TTSProfile`。
- `server/ws_server.py`：`create_app(config=None, tts_client=None)`（**`stt_engine=None` 追加**）・lifespan(owns制御)・`SessionState`。
- `common/protocol.py`：`SttBegin`(id/format/auto_tts)・`SttEnd`(id)・`Transcript`(id/text/final)・`AudioBegin`(id/**parent_id**/format)・`parse_message`。
- `common/pcm.py`：`_pcm_to_float32(data, encoding, channels)->np.ndarray[-1,1]`。
- **Qwen3-ASR 確認済み API**（`qwen-asr`）:
  - `Qwen3ASRModel.from_pretrained(model, dtype=torch.bfloat16, device_map="cuda:0", attn_implementation="sdpa", max_new_tokens=256)`。
  - `model.transcribe(audio=(samples: np.ndarray, sample_rate: int), language="Japanese") -> List[ASRTranscription]`、`result[0].text`。
  - **`transcribe` 内部の `normalize_audio_input` が mono化・16kリサンプル(librosa)・[-1,1]正規化を実施** → サーバ側で soxr 不要。任意 sample_rate を渡してよい。

## 3. 設計

### 3.1 STT エンジン抽象（`server/stt/engine.py` 新規）
- `class STTEngine(Protocol)`: `def transcribe(self, samples: np.ndarray, sample_rate: int, language: str) -> str`。
- `class QwenSTTEngine`:
  - `__init__(self, cfg: QwenLocalConfig)`: **遅延 import**（`import torch`, `from qwen_asr import Qwen3ASRModel`）。`dtype` 文字列→`{"bfloat16":torch.bfloat16,"float16":torch.float16,"float32":torch.float32}`。`Qwen3ASRModel.from_pretrained(cfg.model, dtype=..., device_map=cfg.device, attn_implementation=cfg.attn_implementation, max_new_tokens=256)`。
  - `transcribe(samples, sample_rate, language)`: `lang = _LANG_MAP.get(language, language)`; `res = self._model.transcribe(audio=(samples, sample_rate), language=lang)`; `return res[0].text if res else ""`。**ブロッキング**（呼び出し側が to_thread）。
- `_LANG_MAP = {"ja":"Japanese","en":"English","zh":"Chinese"}`（config `ja`→`"Japanese"` を固定。未知はそのまま渡す）。
- `server/stt/__init__.py` から `STTEngine`/`QwenSTTEngine` を re-export。

### 3.2 ライフサイクル / 注入（`server/ws_server.py`・S1 の TTSClient と同型）
- `create_app(config=None, tts_client=None, stt_engine=None) -> FastAPI`。
- `_owns_stt = stt_engine is None`。`app.state.stt_engine = stt_engine`（注入 or None）。
- **GPU直列化は単一ワーカー executor（codex#1）**: `app.state.stt_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)` を create_app 本体で生成（注入経路でも参照可能）。`asyncio.Lock` は使わない（`to_thread`/Lock だとキャンセル時に lock が解放され、止まらない旧推論と次の推論が GPU で競合するため）。lifespan shutdown で `app.state.stt_executor.shutdown(wait=False)`。
- lifespan:
  - `_owns_client` のとき `TTSClient()` 生成（既存）。
  - **`_owns_stt` のとき（codex#7）**: `cfg.stt.backend == "qwen_local"` のみ `from .stt.engine import QwenSTTEngine; app.state.stt_engine = QwenSTTEngine(cfg.stt.qwen_local)` を生成（重い・GPUロード）。**それ以外の backend は `RuntimeError` で起動失敗（fail fast）**。注入時は生成しない（backend 判定もしない）。
- `SessionState` に **`stt_engine`・`stt_executor`・`language`(=cfg.stt.language)** を追加。`ws_endpoint` で `SessionState(..., stt_engine=app.state.stt_engine, stt_executor=app.state.stt_executor, language=cfg.stt.language)`。
- `server/app.py: run_server()` は変更なし（`create_app(cfg)` のまま。lifespan が STT を持つ）。

### 3.3 Session 状態の拡張（`server/orchestrator.py`）
追加属性: `active_stt_id: str|None`、`stt_buffer: bytearray`、`stt_format: AudioFormatPayload|None`、`stt_auto_tts: bool`、`active_parent_id: str|None`。
**busy の定義（単一アクティブ）**: `self._busy() == (active_stt_id is not None) or (active_task is not None and not active_task.done())`。`speak`/`stt_begin` はともに busy 時に side-band `error(id=msg.id,"busy: stream already active")`。
**STT音声サイズ上限（秒数ベース・codex#6）**: `_STT_MAX_SECONDS = 60`。上限 byte は capture の `stt_format` から計算: `max_bytes = int(_STT_MAX_SECONDS * fmt.sample_rate * bytes_per_sample(fmt.codec) * fmt.channels)`（16k/mono/s16 なら ~1.92MB/60s）。超過で terminal error＋capture 破棄。
**capture リセット helper（codex#9）**: `_reset_stt_capture(self)` = `active_stt_id=None; stt_buffer=bytearray(); stt_format=None; stt_auto_tts=False`。capture を畳む箇所（サイズ超過・cancel・stt_end のスナップショット後）はすべてこれで戻す。

### 3.4 受信・ルーティング（codex S1#9 の踏襲＋binary対応）
- `_receive_message`: text 経路は不変。**binary は `error` ではなく `BinaryFrame(data: bytes)`**（新規 dataclass, orchestrator内）を返す。
- `_message_loop` の分岐:
  - `BinaryFrame` → `_handle_binary(frame.data)`。
  - `SttBegin` → `_handle_stt_begin`。
  - `SttEnd` → `_handle_stt_end`。
  - `Speak` → `_handle_speak`（busy 判定を `_busy()` に変更）。
  - `Cancel` → `_handle_cancel`（STT/TTS 両対応に拡張）。
  - `Transcript`/`SttBegin以外のSTT系で不正` 等 → `error(id=None,"unsupported")`（不明 type は従来通り）。

### 3.5 stt_begin / binary / stt_end
- `_handle_stt_begin(msg: SttBegin)`:
  - `not msg.id` → `error(id=None,"stt_begin.id is required")`。
  - `_busy()` → `error(id=msg.id,"busy: stream already active")`（side-band・**重複 stt_begin 拒否を含む**）。
  - else: `active_stt_id=msg.id`、`stt_buffer=bytearray()`、`stt_format=msg.format`、`stt_auto_tts=msg.auto_tts`。**応答メッセージは無し**（capture 開始）。
- `_handle_binary(data: bytes)`:
  - `active_stt_id is None` → `error(id=None,"no active stt stream")`（side-band）。捨てる。
  - else: `stt_buffer += data`。`len(stt_buffer) > max_bytes`（§3.3）→ `error(id=active_stt_id,"stt audio too large")` ＋ `_reset_stt_capture()`。
- `_handle_stt_end(msg: SttEnd)`:
  - **`msg.id != active_stt_id` → `error(id=msg.id,"no active stt stream")`（side-band・codex#5）。既存 `active_stt_id` の capture は破壊しない（継続）**。
  - else: capture 確定。**PCM 端数を full-frame に切詰め（codex#4）**: `frame_bytes = bytes_per_sample(stt_format.codec) * stt_format.channels`; `usable = len(stt_buffer) - (len(stt_buffer) % frame_bytes)`; `buf = bytes(stt_buffer[:usable])`（端数 byte は破棄）。スナップショット（buf, fmt=stt_format, auto=stt_auto_tts, stt_id=active_stt_id）。`_reset_stt_capture()`（capture 終了）。`active_stream_id=stt_id`、`active_parent_id=None`、`active_task=create_task(self._process_stt(stt_id, buf, fmt, auto))`。

### 3.6 _process_stt（transcribe → transcript →（auto_tts）TTS）
```
async def _process_stt(self, stt_id, buf, fmt, auto_tts):
  loop = asyncio.get_running_loop()
  try:
    samples = _pcm_to_float32(buf, fmt.codec, fmt.channels)   # buf は full-frame 切詰め済(§3.5)。空なら空配列
    if samples.size == 0:
      await self._send_model(Transcript(id=stt_id, text="", final=True))
    else:
      # GPU直列化は単一ワーカー executor（codex#1）。cancel しても実推論は止まらないが、
      # 次の submit は同じ worker のキューに並ぶため GPU 直列性は保たれる。
      text = await loop.run_in_executor(
          self.state.stt_executor,
          self.state.stt_engine.transcribe, samples, fmt.sample_rate, self.state.language)
      await self._send_model(Transcript(id=stt_id, text=text, final=True))
      if auto_tts and text.strip():
        await self._run_auto_tts(stt_id, text)                # 下記
  except asyncio.CancelledError:
    await self._send_stopped_best_effort(self.active_stream_id or stt_id)
    raise
  except Exception as exc:
    await self._send_model(ErrorMsg(id=stt_id, message=repr(exc)))
  finally:
    task = asyncio.current_task()
    if self.active_task is task:
      self.active_task=None; self.active_stream_id=None; self.active_parent_id=None
```
- `_run_auto_tts(stt_id, text)`:
  - `profile = load_profile(profiles_dir, cfg.tts.active_profile)`（失敗→`error(id=stt_id,...)`、return）。
  - `tts_id = f"tts-{stt_id}"`、`self.active_stream_id=tts_id`、`self.active_parent_id=stt_id`。
  - `await self._stream_tts(tts_id, profile, text, parent_id=stt_id, manage_active=False)`（**同タスク内 await・nestしない**）。
- **`_stream_tts` 正式シグネチャ（codex#2）**: `async def _stream_tts(self, stream_id, profile, text, parent_id: str|None=None, manage_active: bool=True)`。
  - `AudioBegin(id=stream_id, parent_id=parent_id, format=fmt)` を送る。
  - **finally の active クリアは `manage_active` でガード**: `if manage_active and self.active_task is asyncio.current_task() and self.active_stream_id == stream_id: active_task=None; active_stream_id=None; active_parent_id=None`。
  - `speak` 経路は既定 `manage_active=True`（従来通り `_stream_tts` が active を管理）。auto_tts 経路は `manage_active=False`（active クリアは `_process_stt` の finally に一元化＝二重/早期クリア防止）。
  - CancelledError は S1 同様 `_send_stopped_best_effort(stream_id)` 後 re-raise（auto_tts 経路では `_process_stt` の except へ伝播し、そこでも stopped を送らないよう注意＝`_stream_tts` が既に送出済みなので `_process_stt` の except の stopped は best-effort で二重でも害なしだが、**`_process_stt` の CancelledError ハンドラは `_run_auto_tts` を経ていない経路（STT推論中 cancel）専用**と考え、TTS 中 cancel は `_stream_tts` の stopped が先行する）。

### 3.7 cancel（STT/TTS 両対応）
- `_handle_cancel(msg)`:
  - **capture 中**（`msg.id == active_stt_id`）: `_reset_stt_capture()` → `Stopped(id=msg.id)` 送信。
  - **処理中**（`active_task` 生存）かつ `msg.id in {active_stream_id, active_parent_id}`: `active_task.cancel()`。`Stopped` は `_process_stt`/`_stream_tts` の CancelledError ハンドラが **`id=active_stream_id`** で best-effort 送信する（codex#3）。
    - STT 推論中の cancel → `active_stream_id == stt_id` なので `stopped(id=stt_id)`。
    - auto_tts の TTS 中の cancel → `active_stream_id == "tts-<sttid>"` なので `stopped(id="tts-<sttid>")`。**クライアント(C2)は `audio_begin.parent_id` で stt_id ↔ tts-id を対応付けて停止判定する**（受け入れ基準に明記）。
  - いずれにも該当せず → `error(id=msg.id,"no active stream")`。

### 3.8 handshake / features
- `ready.features = {"auto_tts": True}`。他は S1 と同じ（codec/tts/profiles/limits/server_time）。

### 3.9 フォーマット
- 入力音声は `stt_begin.format`（C2 のマイクは 16000/mono/pcm_s16le 想定だが、**任意 sample_rate を許容**＝Qwen が内部 16k 化）。`_pcm_to_float32` で float32 [-1,1] にして `(samples, fmt.sample_rate)` を渡す。
- 出力 TTS は S1 同様 profile.audio（GPT-SoVITS 32000/mono）。

## 4. 受け入れ基準
- [ ] `create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt)` の TestClient で：`hello`→`ready`（**`features.auto_tts==True`**）。
- [ ] `stt_begin(id=u2,auto_tts=false)`→binary×N→`stt_end(u2)` で `transcript(id=u2, text=<fake>, final=true)`。**TTS は流れない**。
- [ ] `stt_begin(id=u2,auto_tts=true)`→binary→`stt_end` で `transcript(u2)` の後に `audio_begin(id="tts-u2", parent_id="u2")`→binary→`audio_end(tts-u2)`。
- [ ] capture 中の 2回目 `stt_begin` / `speak` は `error(id=B,"busy...")`（side-band, capture継続）。
- [ ] active 無しでの binary は `error(id=None,"no active stt stream")`。**`stt_end` の id 不一致は `error(id=msg.id,"no active stt stream")`（side-band）で、進行中の別 capture は壊れない**（codex#5）。
- [ ] `cancel(u2)` が **capture 中**なら `stopped(id=u2)`、**STT 推論中**なら `stopped(id=u2)`（active_stream_id=stt_id）、**auto_tts の TTS 中**なら `stopped(id="tts-u2")`（active_stream_id）でHTTPもキャンセル。いずれも active が解放され、後続リクエストが busy 解除後に通る。
- [ ] **STT 推論中の cancel（codex#8）**: FakeSTTEngine をブロックさせ、`cancel(stt_id)` で `stopped(stt_id)`・active 解放・次リクエスト疎通を検証。
- [ ] STT 推論が **単一ワーカー executor** で直列化（イベントループを長時間ブロックしない）。注入 stt_engine 使用時、lifespan は実モデルを生成しない。**owned 起動で backend!="qwen_local" は起動失敗**（codex#7）。
- [ ] 空音声（binary 0 byte / 端数のみ）で `transcript(text="")`、auto_tts でも空テキストは TTS を起こさない。**端数 byte は full-frame に切詰め**（codex#4）。
- [ ] `speak` 単体（S1 機能）が回帰しない。`AudioBegin.parent_id` は speak 経路では None、auto_tts 経路では stt_id。
- [ ] AST境界（server→PySide6/sounddevice 禁止）・py_compile・既存pytest 全green（**`tests/test_ws_tts.py` の ready 期待値を `features.auto_tts==True` に更新**・codex#10）。

## 5. 変更/新規ファイル
- `server/stt/engine.py`（**新規**: `STTEngine`/`QwenSTTEngine`/`_LANG_MAP`）
- `server/stt/__init__.py`（re-export）
- `server/ws_server.py`（`create_app(..., stt_engine=None)`・lifespan で STT 所有(backend判定)・`SessionState(stt_engine/stt_executor/language)`・`app.state.stt_executor`(ThreadPoolExecutor max_workers=1)・shutdownで executor.shutdown）
- `server/orchestrator.py`（`SessionState` 拡張・`BinaryFrame`・STT 状態・`_reset_stt_capture`・`_handle_stt_begin/_handle_binary/_handle_stt_end/_process_stt/_run_auto_tts`・`_busy`・`_handle_cancel` 拡張・`_stream_tts(parent_id, manage_active)`・`features.auto_tts=true`・`_cleanup` で STT も停止）
- `tests/test_ws_stt.py`（**新規**）
- `tests/test_ws_tts.py`（**既存修正**: `ready` の `features.auto_tts` 期待値を `True` に更新・codex#10）

## 6. テスト / 検証
- **単体（必須・GPU不要）** `tests/test_ws_stt.py`：
  - `FakeSTTEngine`: `transcribe(samples, sr, language)` が固定 text（例 "こんにちは"）。受け取った `samples.shape`/`sr` を記録（フォーマット検証）。
  - `FakeTTSClient`: S1 の `test_ws_tts.py` と同様（2chunk以上 yield）。
  - `TestClient(create_app(cfg, tts_client=fake_tts, stt_engine=fake_stt))` で §4 を検証（websocket_connect、`send_bytes` で binary 送信）。
  - 注入時 lifespan が実 STT/TTS を生成しないこと。
- **統合（実 Qwen・任意）**: 小スクリプトで実サーバへ 16k mono PCM（既存 wav を 16k 化）を stt_begin→binary→stt_end し、`transcript` と STT 時間(~0.29s)・auto_tts の音声を確認。
- AST境界・py_compile・既存pytest 全green維持。

## 7. リスク・留意点
- **GPU直列化と cancel 意味論（codex#1）**: `run_in_executor`/`to_thread` で投げた Qwen 推論は **cancel しても実スレッド上では止まらない**（torch 推論は割込み不可）。`asyncio.Lock` だと cancel で lock が即解放され、止まらない旧推論と次の推論が GPU で競合する。→ **単一ワーカー `ThreadPoolExecutor(max_workers=1)`** にして、次の submit が同 worker のキューに並ぶことで GPU 直列性を保証する（lock を使わない）。cancel された await は結果を捨てるだけで、裏の推論は1件完走してから次が走る。
- **lifespan の重い起動**: 実 STT モデルロードは数秒＋VRAM。テストは必ず `stt_engine` 注入で回避。`uv run` 実起動時のみロード。
- **active クリアの二重化**: auto_tts 経路は `_process_stt` タスク内で `_stream_tts` を await 実行するため、active 状態クリアは **`_process_stt` の finally に一元化**（`_stream_tts(manage_active=False)`）。speak 経路は従来通り `_stream_tts` が管理。
- **cancel の id 整合**: クライアント（C2）は stt の id（例 u2）で cancel する。サーバは capture 中=`active_stt_id`、auto_tts TTS 中=`active_parent_id`(=u2) で照合する（`active_stream_id`=tts-u2 とは別に parent も見る）。
- **binary 帰属**: S2 では binary は「直近の未終了 `stt_begin` の capture」に属する。side-band JSON は帰属を変えない（S1 規則の踏襲）。
- **言語マッピング**: config `ja` → Qwen `"Japanese"`（`_LANG_MAP` で固定。未知言語はそのまま渡し Qwen 側のバリデーションに委ねる）。
- **torchaudio 不要**: Qwen は内部で librosa を使い resample/mono化する（`qwen-asr` 依存に含まれる）。server extra への追加は不要。
- C2向け: クライアントは `stt_begin(id, format=16k/mono/pcm_s16le, auto_tts=true)` → VAD 区切りの PCM を binary 送信 → `stt_end` → `transcript`/（auto_tts）音声。本書はサーバ側のみ。
