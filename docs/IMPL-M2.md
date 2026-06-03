# M2 実装計画書 v2: ストリーミング再生 ＋ 音声I/O

> 更新: v1 → **v2: codex(gpt-5.5) レビュー反映**（→ `docs/REVIEW-codex-M2.md`）。停止時のHTTPタスクcancel、`_build_request`の共有化、tail drain、24bit禁止、仮想sink候補選択規則、同時発話ポリシー等を明文化。
> フロー: 本書 → 実装は SecondOpinion(GLM-5.1) に委託 → 自分で検証/統合。
> 対象: `/mnt/holyland/Project/Text-to-shaberi`（Python3.12 / uv / PySide6 / qasync）。**この仕様の通りに実装すること（曖昧な箇所は本書の規定に従う）。**

## 1. 目的・スコープ
M1 の「全受信→一括再生」を **チャンク逐次再生** に置き換え、TTFA を短縮（§3 SLO, 狙い ~270ms）。出力デバイス切替の実用化、停止ボタン、仮想sink `TTS_Virtual`(ttsvirt) への配信ルーティング(best-effort)。
非スコープ: キュー（M3）、マイク/STT（M4/M5）。ただし停止/abort と HTTPタスクcancel は M3 の素地として正しく作る。

## 2. 現状(M1)の関連実装（変更の起点）
- `tts/client.py`: `TTSClient.synthesize(profile, text) -> AudioBuffer`、`_build_request(profile, body)->httpx.Request`、`_decode_raw/_decode_wav`、`_PCM_DTYPE`、`AudioBuffer`。
- `audio/player.py`: `play(samples, sample_rate, device)`、`_device_samplerate(sd, device)`、`stop()`。
- `audio/devices.py`: `preferred_default_output()`、`list_output_devices()`、`resolve_device()`。
- `ui/main_window.py`: 出力先 combo（"既定…"=None / 各デバイス index）、`set_on_speak`、`selected_output_device()->int|None`、`log()`。
- `app.py: run_gui`: `speak(text)` が `synthesize`→`run_in_executor(play)`、`app.aboutToQuit`→`client.aclose()`。
- プロファイル実値: `response.mode=audio_stream`, `audio.encoding=pcm_s16le`, `sample_rate=32000`, `channels=1`, `streaming_mode=3`, `media_type=raw`。

## 3. 設計（確定仕様）

### 3.1 リクエスト構築の共通化（`tts/client.py`）
- **`_build_request_kwargs(self, profile, body) -> tuple[str, str, dict]`** を新設し、`(method, url, kwargs)` を返す。`kwargs` は `headers`, `timeout`, および body種別に応じて **いずれか1つ**：
  - `profile.method == "GET"` → `params=body`
  - `body_type == "json"` → `json=body`
  - `body_type in ("form","multipart")` → `data=body`（**multipartのファイル送信はM2では未対応**、コメントで明記）
  - その他 → `json=body`
- `synthesize()` は `method,url,kwargs=_build_request_kwargs(...)` → `resp = await self._client.request(method, url, **kwargs)`。
- 既存 `_build_request`(httpx.Request返し) は削除し `_build_request_kwargs` に一本化（呼び出し側を更新）。

### 3.2 ストリーミング受信（`tts/client.py`）
- **`async def stream(self, profile, text, **variables) -> AsyncIterator[bytes]`**
  - 適用条件: `profile.response_mode == "audio_stream"`（呼び出し側が判定。それ以外は `synthesize` を使う）。
  - 実装:
    ```python
    method, url, kwargs = self._build_request_kwargs(profile, profile.render_body(text, **variables))
    async with self._client.stream(method, url, **kwargs) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
    ```
  - **キャンセル安全**: 呼び出し側が `Task.cancel()` した場合、`async with` が確実にHTTP接続を閉じること（受け入れ基準）。

### 3.3 PCMデコード共通化（`tts/client.py`）
- **`_pcm_to_float32(data: bytes, encoding: str, channels: int) -> np.ndarray`** を新設（`_decode_raw` と `StreamingPlayer` で共用）。
  - 対応 encoding と変換:
    - `pcm_s16le`: dtype `<i2`、`/32768.0`
    - `pcm_s32le`: dtype `<i4`、`/2147483648.0`
    - `pcm_f32le`: dtype `<f4`、そのまま
    - **上記以外（`pcm_s24le` 含む）は `raise ValueError(f"unsupported encoding: {encoding}")`。黙って `<i2` にフォールバックしない。**
  - 戻り shape: **mono(channels==1) は 1D `(n,)`、多chは 2D `(-1, channels)`**。`data` は呼び出し側が「完全サンプル境界」に整えて渡す前提（端数は渡さない）。
- **`bytes_per_sample(encoding) -> int`**: s16le=2 / s32le=4 / f32le=4 / それ以外は `ValueError`。
- サンプル境界分割を**純関数**に切り出してテスト可能にする: **`split_full_samples(buf: bytes, frame_bytes: int) -> tuple[bytes, bytes]`** が `(完全フレーム分, 端数carry)` を返す（`frame_bytes = bytes_per_sample*channels`）。
- 既存 `_PCM_DTYPE`/`_decode_raw` は `_pcm_to_float32` を使う形に整理（24bit近似は削除）。`AudioBuffer.samples` のコメントを「mono=1D / 多ch=2D」に修正。

### 3.4 ストリーミング再生（`audio/player.py`）
- **`class StreamingPlayer`**
  - `__init__(self, src_rate:int, channels:int=1, device:int|None=None, encoding:str="pcm_s16le", preroll_ms:int=40)`:
    - `bytes_per_sample(encoding)` を検証（不正なら `ValueError`）。
    - `device is None` → `preferred_default_output()`。
    - `self._target_sr = _device_samplerate(sd, device)`。
    - rate差があれば `self._resampler = soxr.ResampleStream(src_rate, self._target_sr, channels, dtype="float32", quality="HQ")`、無ければ None。
    - `self._stream = sd.OutputStream(samplerate=self._target_sr, channels=channels, dtype="float32", device=device, latency="low")`; `self._stream.start()`。
    - `self._carry = b""`、`self._frame_bytes = bytes_per_sample(encoding)*channels`、`self._preroll = []`（list[np.ndarray]）、`self._preroll_frames = int(self._target_sr*preroll_ms/1000)`、`self._started_audio = False`。
  - **`write_bytes(self, data: bytes) -> None`**:
    1. `full, self._carry = split_full_samples(self._carry + data, self._frame_bytes)`。
    2. `if not full: return`。
    3. `frames = _pcm_to_float32(full, self._encoding, self._channels)`。
    4. `if self._resampler is not None: frames = self._resampler.resample_chunk(frames)`。
    5. `if len(frames)==0: return`。
    6. **プリロール吸収**: `started_audio` が False の間は `self._preroll.append(frames)`。合計フレーム数が `self._preroll_frames` 以上になったら連結して1回 `self._stream.write(...)` し `started_audio=True`。以降は各 `frames` を即 `self._stream.write(frames)`。
    7. `OutputStream.write` 例外時は `abort()` して再 raise。
  - **`close(self) -> None`**（正常終了。末尾を切らない）:
    - resampler があれば `tail = self._resampler.resample_chunk(np.empty((0,) if channels==1 else (0,channels), dtype=np.float32), last=True)`。
    - プリロール未flushなら残りを連結。`tail`＋未flush分を write。
    - **tail drain**: `time.sleep(float(self._stream.latency) + 0.05)` で PortAudio バッファを鳴らし切ってから `self._stream.stop(); self._stream.close()`（`stop()` だけに drain を依存しない）。
  - **`abort(self) -> None`**（即停止）: `self._stream.abort(); self._stream.close()`（idempotent に。二重closeはガード）。
- `play()`(M1) は wav/json・非stream用に**据え置き**。

### 3.5 app.py 統合（`run_gui`）
- **起動時（最優先・QApplication生成前かつ sounddevice import/`_load_output_devices()` 前）**: pulse client 名を固定するため
  `os.environ.setdefault("PULSE_PROP", f"application.name=text-to-shaberi media.role=production")` を設定。
- 状態保持: `current_task: list[asyncio.Task|None] = [None]`、`current_player: list[StreamingPlayer|None] = [None]`（クロージャ用に1要素list）。
- `speak(text)`（async）:
  ```python
  device = window.selected_output_device()           # int | None | "__ttsvirt__"
  use_virtual = device == "__ttsvirt__"
  play_device = None if use_virtual else device       # 仮想は pipewire(None→preferred) に流す
  window.set_busy(True)                                # 送信ボタン disable（同時発話防止）
  player = None
  try:
      current_task[0] = asyncio.current_task()
      if profile.response_mode == "audio_stream":
          player = await loop.run_in_executor(None, lambda: StreamingPlayer(
              profile.audio.sample_rate, profile.audio.channels, play_device, profile.audio.encoding))
          current_player[0] = player
          if use_virtual:
              moved = await loop.run_in_executor(None, lambda: move_app_to_sink("ttsvirt", "text-to-shaberi", os.getpid()))
              window.log(f"[route] TTS_Virtual move: {moved}")
          t0 = loop.time(); first_chunk = None
          async for chunk in client.stream(profile, text):
              if first_chunk is None:
                  first_chunk = loop.time()-t0
                  window.log(f"[tts] first-chunk {first_chunk*1000:.0f}ms")
              await loop.run_in_executor(None, player.write_bytes, chunk)
          await loop.run_in_executor(None, player.close)
          window.log(f"[tts] 再生完了 {loop.time()-t0:.2f}s")
      else:
          buf = await client.synthesize(profile, text)
          await loop.run_in_executor(None, play_audio, buf.samples, buf.sample_rate, play_device)
  except asyncio.CancelledError:
      if player is not None:
          await loop.run_in_executor(None, player.abort)
      window.log("[tts] 停止しました")
      # 正常停止扱い。再raiseしない。
  except Exception as exc:
      if player is not None:
          await loop.run_in_executor(None, player.abort)
      window.log(f"[error] {exc!r}")
  finally:
      current_task[0] = None
      current_player[0] = None
      window.set_busy(False)
  ```
- `on_speak = lambda text: (current_task[0] is None) and asyncio.ensure_future(speak(text))`（busy中は send disable なので二重起動しない設計だが、ガードも入れる）。
- **停止** `on_stop()`: `if current_player[0]: loop.run_in_executor(None, current_player[0].abort)`; `if current_task[0]: current_task[0].cancel()`。
- **aboutToQuit**: `if current_task[0]: current_task[0].cancel()`; `if current_player[0]: player.abort()`; その後 `asyncio.ensure_future(client.aclose())`。

### 3.6 出力デバイス切替・同時発話ポリシー
- 各発話で `selected_output_device()` を都度参照（切替は次発話から反映）。combo変更時に再生中なら「次発話から切替わります」をログ（任意）。
- **同時発話**: M2は「再生中は送信ボタンを無効化」。`MainWindow.set_busy(bool)` を追加し、送信中は `text_input`/`send_button` を disable、`stop_button` のみ有効。完了/停止/エラーで解除。

### 3.7 仮想sink ルーティング（`audio/routing.py` 必須・新規）
- **`move_app_to_sink(sink_name: str, app_name: str, pid: int) -> int | None`**（移動した sink-input id、失敗時 None）:
  1. `shutil.which("pactl")` 無→ログ「pactl不在」→ None。
  2. `pactl list short sinks` に `sink_name`(ttsvirt) 無→ログ「sink不在」→ None。
  3. `pactl list sink-inputs` を `subprocess.run([...], capture_output=True, text=True, timeout=3)`（**shell不使用**）で取得し、各 "Sink Input #N" ブロックの `application.process.id` と `application.name` を抽出。
  4. 候補 = `application.process.id == str(pid)` **かつ** `application.name == app_name`。
     - 0件 → ログ「sink-input未検出」→ None。
     - 複数 → **最大の#id（最も新しい）** を採用しログに候補数を出す。
  5. `pactl move-sink-input <id> <sink_name>`（timeout付）。成功→id返す。失敗→ログ→None。
- すべて例外を捕捉し**クラッシュさせない**。各失敗を個別ログ。
- 注意: sink-input は **OutputStream.start() 後** に出現するため、`move_app_to_sink` は再生開始直後（最初の write 前後）に呼ぶ。PULSE_PROP は §3.5 の通り起動時設定。

### 3.8 UI（`ui/main_window.py`）
- 「停止」ボタン追加、`set_on_stop(callback)`。
- 出力先 combo に項目 **「TTS_Virtual（配信用）」**（`addItem(..., "__ttsvirt__")`）追加。
- `selected_output_device() -> int | None | str`（戻り型を更新）。
- `set_busy(busy: bool)`: 送信中UIの有効/無効。

## 4. 受け入れ基準
- [ ] 既定デバイスで streaming 再生。`first-chunk` をログ表示（≤400ms, 狙い~270ms）。
- [ ] underrun/境界ノイズ無く**末尾まで**再生（短いsineで末尾欠け無しを手動確認）。
- [ ] 出力先デバイス変更が次発話で反映。
- [ ] 「停止」で ~150ms 程度で無音化し、**HTTPストリームも閉じる**（task cancel）。閉じた stream への write 例外でクラッシュしない。
- [ ] 再生中は送信ボタンが無効（同時発話で音が重ならない）。
- [ ] 「TTS_Virtual」選択で OBS等の「Monitor of TTS_Virtual」に流れる（best-effort、失敗時は既定sink＋警告ログ・**非クラッシュ**）。
- [ ] アプリ終了時に進行中 task/player を止めてから終了。
- [ ] `py_compile` 全モジュール成功、後述ユニットテスト成功。

## 5. 変更/新規ファイル
- `tts/client.py`（`_build_request_kwargs`/`stream`/`_pcm_to_float32`/`bytes_per_sample`/`split_full_samples`、`_decode_raw`整理）
- `audio/player.py`（`StreamingPlayer` 追加）
- `audio/routing.py`（**新規・必須**：`move_app_to_sink`）
- `ui/main_window.py`（停止ボタン/`set_on_stop`/`set_busy`/TTS_Virtual項目/`selected_output_device`型更新）
- `app.py`（streaming統合、停止結線、PULSE_PROP、仮想sink移動、aboutToQuit強化）
- `tests/test_pcm.py`（**新規**：`_pcm_to_float32` と `split_full_samples` のユニットテスト）

## 6. テスト
- **ユニット（sounddevice不要・必須）**: `tests/test_pcm.py`
  - `split_full_samples(b"\x01\x02\x03", 2) == (b"\x01\x02", b"\x03")`、奇数byte分割でcarryが繰越されること。
  - `_pcm_to_float32` が s16le/s32le/f32le を正しく正規化し、mono=1D/2ch=2D を返すこと。未知encodingで `ValueError`。
- **ヘッドレス（GPT-SoVITS :9880 起動前提）**: `client.stream` で first-chunk 時刻を計測（~270ms）。`py_compile src/**/*.py`。
- **GUI（ユーザー確認）**: 実音・first-chunk表示・末尾欠け無し・デバイス切替・停止(無音化&task cancel)・送信disable・TTS_Virtual→OBS。

## 7. 留意点
- `run_in_executor` の write は順序保証のため**逐次 await**。
- soxr mono入力は 1D、多chは (-1,channels)。
- `OutputStream(latency="low")`。プリロール ~40ms でジッタ吸収。tail は sleep(latency+0.05) で drain。
- routing は環境依存で best-effort。確実版（`pw-link`/`node.target`）は **M2b** に分離（本M2では非ブロッキングで失敗許容）。
- subprocess は timeout・`capture_output=True`・`text=True`・shell不使用。
