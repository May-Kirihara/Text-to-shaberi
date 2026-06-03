# C2 codex レビュー指摘（gpt-5.5・read-only・実コード突合）

> 対象: `docs/IMPL-C2.md` v1。総評: **条件付きGO**（critical×3 / should×6 / nit×3）。
> 3つの critical（channels=1 契約 / STT送信シーケンスの atomic 化 / MicCapture 停止順序）を直せば実装可、とのこと。全12点を v2 に反映する。

## critical
1. **§3.9/app.py — channels=1 契約**: `MicCapture` は多ch入力を mono 化するのに、`SttBegin.format.channels` に `cfg.mic.channels` を送る設計。`cfg.mic.channels=2` だとサーバ `_pcm_to_float32(buf, codec, channels)` が mono bytes を 2ch 解釈し波形破壊。→ **STT送信フォーマットは常に `channels=1` 固定**。`MicCapture` 出力契約も「常に int16 mono bytes」と明記。
2. **§3.4 — STT送信シーケンスの atomic 化**: `_send_lock` を1 sendごとに掛けるだけだと `utter` の `stt_begin`→binary→`stt_end` の途中に `cancel()` が割り込め、S2 は capture 中 cancel で `_reset_stt_capture()` するため残り binary/stt_end が `no active stt stream` を誘発しライフサイクルが乱れる。→ **送信シーケンス全体を同一ロックで囲む**（begin〜end を atomic 送出）。`cancel()` はその後に送らせ、S2 に「STT推論中 cancel」として処理させる。
3. **§3.3 — MicCapture.stop() 順序**: 「join してから stream stop/close」だと `read()` がデバイス都合で返らない時に Qt メインスレッドを最大2秒止め、join timeout 後に worker 生存のまま close する競合。→ **stop flag → `stream.abort()`/`stop()` で read を起こす → join → close**。GUI終了時は `asyncio.to_thread(mic.stop)` も検討しメインループを塞がない。

## should
4. **§3.2 — VADパラメタ検証**: 不正値（`sample_rate=44100`/`frame_ms=25`）で実行時に落ちる。→ `VadSegmenter.__init__` で `sample_rate∈{8000,16000,32000,48000}`・`frame_ms∈{10,20,30}` を validate し `ValueError`。`--check` でも検出可に。
5. **§3.2 — max_frames 分割の連続性**: 「即 idle・preroll 継続」では連続発話中に次セグメント開始まで `onset_frames` 分を取りこぼす可能性。「取りこぼさない」は過大。→ 強制確定後、発話継続中は**末尾有声フレームを次セグメントの preroll に残す/即新セグメント開始**。テストに「max_speech 分割後も連続 speech が次セグメントに入る」を追加。
6. **§3.4 — terminal error の id 区別**: `error(id="tts-U")`(auto_tts TTS失敗) と `error(id=U)`(STT失敗 or auto_tts プロファイルロード失敗) を同じ表示に潰している。→ terminal 時に `id==_playback_id` と `id==_active_id` を区別してログ。受け入れに `error(id=U)` プロファイルロード失敗ケースを追加。
7. **§3.7 — busy-skip ログのスパム**: 常時録音で自分の TTS 再生を VAD が拾い続けログが溢れる。→ busy 中は MicCapture を一時 pause するか、**skip ログをレート制限（≤1/s）**。
8. **§3.4 — auto_tts feature 確認**: `ready.features.auto_tts=True` 前提だが未確認。S1/旧サーバ接続時の失敗経路が曖昧。→ `connect()` 後に `_ready.features.get("auto_tts")` を確認し、false ならマイクトグル無効化 or 認識のみへ降格＋明示ログ。
9. **§4 — server error(id=U) capture-reset ケース**: WSClient テストに「`stt_begin` 後にサーバが `error(id=U,"stt audio too large")` を返す→client が idle 化し次発話を送れる」が抜け。→ fake サーバに追加。

## nit
10. **§3.6 — 絵文字ボタン**: Windows/PySide でフォント差。→ `🎤` 許容、accessibleName/tooltip 付与、崩れ時 `"Mic"` fallback。
11. **§3.5 — config 防御**: `raw["mic"]` が dict でない場合の防御なし。→ `mic_raw = raw.get("mic") or {}` 化。
12. **§3.8 — 「<10ms」表現**: 楽観的。→「転送時間は STT/TTS 推論に比べ小さい」に弱める。

## 反映方針（v2）
全12点を反映。特に critical 3点（channels=1 固定 / 送信 atomic 化 / 停止順序）と should#7（ログ抑制）・#8（auto_tts降格）はコード挙動に直結するため明文化。
