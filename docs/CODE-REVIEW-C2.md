# C2 コードレビュー / 検証記録

> 対象: C2「クライアント: マイク常時録音 + webrtcvad 発話区切り + サーバSTT + auto_tts 再生 + 最小バックプレッシャ」（GLM-5.1 実装 → 私が検証・統合・修正）。
> 仕様: `docs/IMPL-C2.md` v2（codexレビュー `docs/REVIEW-codex-C2.md` の12点反映済み）。

## 1. 実装サマリ（GLM）
| ファイル | 内容 | 判定 |
|---|---|---|
| `client/audio/vad.py` | `VadSegmenter`（webrtcvad 遅延/注入・ノイズゲート=判定の床・残差バッファ・idle/speaking 状態機械・**sample_rate/frame_ms 検証ValueError**・**max_speech 強制確定後の連続継承(末尾padでseed)**・`feed/flush/reset`） | ✅ 仕様準拠 |
| `client/audio/mic.py` | `MicCapture`（InputStream ブロッキング read・**mono int16 出力契約**・`stream/segmenter_factory` 注入・sounddevice 遅延・overflow ログ・`call_soon_threadsafe`・**stop=abort→join→close 順**） | ✅ 仕様準拠 |
| `client/ws_client.py` | `utter`（**stt_begin〜stt_end を単一ロックで atomic 送出**・**channels=1 固定**・auto_tts 降格・rollback）・`_ws_send`(非ロック)・`_send`(ロック)・`is_busy`・auto_tts 連鎖ディスパッチ（`parent_id` 受理・`_playback_id`・transcript・id区別 error・両id終端）・cancel は常に `Cancel(_active_id)` | ✅ 仕様準拠 |
| `client/config.py` | `VADConfig`+aggressiveness/frame_ms/min_speech_ms/max_speech_ms、`MicPreprocessConfig`+gate_threshold、`raw.get("mic") or {}` 防御 | ✅ |
| `client.toml` | `[mic.vad]`/`[mic.preprocess]` 追記 | ✅ |
| `client/ui/main_window.py` | 🎤 トグル（checkable・accessibleName・tooltip）・`set_on_mic_toggle`/`set_mic_active`/`set_mic_enabled` | ✅ |
| `client/app.py` | MicCapture 配線・`on_mic_toggle`/`on_segment`（**busy-skip ログ ≤1/s**）・`stt_format`・auto_tts 非対応でトグル無効化・終了で `to_thread(mic.stop)` | ✅（私が1点修正） |
| `tests/test_vad_segmenter.py`(13)・`tests/test_mic_capture.py`(8)・`tests/test_ws_client.py`(+9) | fake VAD/stream/segmenter/sink 注入。実依存なし | ✅ |

codex 12指摘（critical×3/should×6/nit×3）はすべて実装/テストに反映を確認:
①channels=1 固定（`test_channels_always_1`）・②atomic 送出（単一 `async with self._send_lock`、`test_utter_atomic_send_cancel_after_stt_end`）・③stop 順序 abort→join→close・④VAD パラメタ検証 ValueError・⑤max 分割の連続継承・⑥error id 区別ログ（`test_stt_error_terminal`/`test_auto_tts_error_terminal`）・⑦busy-skip ログ ≤1/s・⑧auto_tts 降格（`test_auto_tts_degradation`）・⑨capture-reset 後に次 utter 可（`test_capture_reset_error_then_next_utter`）・⑩🎤 accessibleName/tooltip・⑪config dict 防御・⑫文言緩和。

## 2. 私による修正
- **`app.py`: `loop.aboutToQuit` → `app.aboutToQuit`**。`qasync.QEventLoop` には `aboutToQuit` 属性が**存在しない**（`hasattr`=False を確認）ため、GUI 起動時に `AttributeError` でクラッシュする実バグ。終了フックは `QApplication`(=`app`) のシグナルに接続するのが正。ユニットテストは `run_gui` を呼ばないため検出されず、`--check`/実機 GUI 起動で初めて顕在化する経路だった。

## 3. 検証結果
- **ユニット**: `uv run pytest -q` **115 passed**（S2の84 + VAD13 + Mic8 + ws_client STT/auto_tts 9 + その他）。**3回連続クリーン**・OOM/ハング無し。GPU 不変（全テスト fake 注入で実 webrtcvad/sounddevice/Qwen を読まない）。
- **import境界**: `tests/test_import_boundaries.py` green。新規 `vad.py`/`mic.py` は **webrtcvad/sounddevice をトップレベル import しない**（factory 内/関数内遅延）を grep でも確認。
- **py_compile**: 新規/変更ファイル全 OK。`--check` で新 config フィールド込みのロード成功。
- **実サーバ end-to-end（Linux・実 webrtcvad・実 Qwen3-ASR・実 GPT-SoVITS）** `/tmp/c2_itest.py`:
  - ref.wav(48k mono, 5.77s) → 16k mono int16 → **実 VadSegmenter が 2 セグメント(2.49s/3.24s)に分割**（文間 >500ms 無音で正しく区切り）。
  - 最長セグメントを `WSClient.utter(auto_tts=True)` で実サーバへ（**SttBegin.format.channels=1**）。
  - **transcript** = 「掘ってくるモンスターを逆襲して食べることができる。」（送出セグメントに対し正確な日本語認識）。
  - **auto_tts 連鎖** = `audio_begin{parent_id}`→binary→`audio_end`、sink begin/feed×6/end、音声 215034B(~3.36s @32k mono)。
  - **TTFA(発話送出→初音) 1067ms**、状態遷移 ready→connected→busy→transcript→idle クリーン。**RESULT PASS ✅**。

## 4. スレッド/契約の要点（再掲・検証済み）
- 取り込み worker は `InputStream.read()` ブロッキング、main へは `call_soon_threadsafe` のみ。`stop()` は abort で read を起こしてから join→close（Qt メインスレッドを長く塞がない）。
- `utter` の `stt_begin`〜`stt_end` は1ロックで atomic。`asyncio.Lock` 非再入のためロック内は `_ws_send`(非ロック版)。cancel の `Cancel` はシーケンス後にのみ割り込み、S2 が「STT推論中 cancel」として `stopped` を返す。
- STT送信は常に **channels=1**（MicCapture が mono 化）。サーバ `_pcm_to_float32` の多ch誤解釈による波形破壊を防止。
- auto_tts 受信音声は `id="tts-U"`/`parent_id=U`。クライアントは `parent_id==_active_id` で受理し `_playback_id` を別管理。空 transcript はサーバと同条件（`auto_tts and text.strip()`）で idle 化し固まらない。

## 5. 未対応・後続へ
- **マイク pause / AEC / バージイン**（X2）: 現状は busy 中セグメント破棄（ログ ≤1/s）でフィードバック抑止。ヘッドフォン運用前提。
- **入力デバイス選択UI / 適応ノイズフロア / denoise(rnnoise)**（X2）。
- **部分/逐次 transcript（final=false）**: Qwen が full-utterance のため未対応（バックエンド差替時）。
- **Windows 実機**（あなた）: WASAPI 既定マイクで取り込み・無音区切り・認識・auto_tts 再生・🎤 トグルの確認が残課題（headless では実マイク不可）。
- atomic 送信テスト（`test_utter_atomic_send_cancel_after_stt_end`）は cancel を upload 完走後に検査する構造で、並行割り込みを敵対的には再現していない（実装はロックで構造的に保証済み・nit）。
