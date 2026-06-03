# codex レビュー: IMPL-S2（サーバ STT over WebSocket）

> reviewer: codex-cli / gpt-5.5 / sandbox=read-only。対象: docs/IMPL-S2.md v1。反映先: v2。重要度順。

1. **critical**: `asyncio.to_thread` は cancel しても実スレッド上の推論を止めないため、`async with self.state.stt_lock` がキャンセルで即解放されると、古い Qwen 推論が裏で走ったまま次の STT が GPU に入れます（`docs/IMPL-S2.md:88-96`）。  
   **修正案**: `to_thread` の Future/Task を保持し、キャンセル時も STT lock を保持したまま worker 完了まで待つ、または専用単一 worker queue にして GPU 直列性を lock ではなく worker 側で保証する、と明記してください。

2. **critical**: auto_tts の `_stream_tts` 呼び出しが本文内で矛盾しています。`docs/IMPL-S2.md:107` は `_stream_tts(..., parent_id=stt_id)` のみ、`docs/IMPL-S2.md:109` は `manage_active=False` 必須としています。現行 S1 の finally は `active_task is current_task and active_stream_id == stream_id` で消すため、auto_tts 同一タスク await では実際に二重/早期クリアします（`orchestrator.py:228-232`）。  
   **修正案**: `_stream_tts(..., parent_id=None, manage_active=True)` を正式シグネチャにし、auto_tts は必ず `manage_active=False`、speak は既定 `True` と明記してください。

3. **should**: auto_tts 中 cancel の `Stopped.id` が曖昧です。計画は C2 が `stt` id で cancel するとしつつ、送信 id は `active_stream_id` と書いており、auto_tts 中は `tts-<sttid>` になります（`docs/IMPL-S2.md:111-115`, `155-156`）。  
   **修正案**: `cancel(u2)` に対して `stopped(id="tts-u2")` を返すのか、`stopped(id="u2")` も返すのかを固定し、C2 が `parent_id` で照合する前提まで受け入れ基準に追加してください。

4. **should**: `_pcm_to_float32` は `channels > 1` で `reshape(-1, channels)` するため、端数 bytes や channel 境界不一致で `ValueError` になります（`common/pcm.py:33-46`）。計画の「空なら空配列」だけでは不正長 PCM の扱いが未定義です（`docs/IMPL-S2.md:84`）。  
   **修正案**: `bytes_per_sample(fmt.codec) * fmt.channels` で `len(buf)` を検証し、端数は terminal `error(id=stt_id, "invalid pcm frame size")` にするか、明示的に切り捨てる規則を追加してください。

5. **should**: `stt_end` の id 不一致は「no active stt stream」になっていますが、別 id の capture は実際には active です（`docs/IMPL-S2.md:76-78`）。ここで実装者が capture を壊すと binary 帰属が崩れます。  
   **修正案**: `error(id=msg.id, "no active stt stream")` は side-band で、既存 `active_stt_id` の capture は継続、と明記しテスト化してください。

6. **should**: STT 音声サイズ上限の記述が不正確です。`32*1024*1024` は約 32MiB で、16kHz/mono/s16 なら約17分です。`~16MB超` という説明とも合いません（`docs/IMPL-S2.md:56`）。  
   **修正案**: 上限値と意図を合わせてください。短時間暴走防止なら例えば秒数ベースで `max_stt_audio_seconds` 相当を計算する方が明確です。

7. **should**: `stt_engine` 注入/所有権は概ね良いですが、`cfg.stt.backend` が `qwen_local` 以外の時の挙動が未定義です（`docs/IMPL-S2.md:45-50`）。  
   **修正案**: owned 起動時は `backend=="qwen_local"` のみ生成、それ以外は起動時エラーまたは handshake error にする、と明記してください。

8. **should**: 受け入れ基準に「STT 推論中の cancel」がありません。capture 中 cancel と auto_tts TTS 中 cancel はありますが、`active_stream_id=stt_id` で Qwen 推論中の経路が抜けています（`docs/IMPL-S2.md:130-131`）。  
   **修正案**: FakeSTTEngine をブロックさせ、`cancel(stt_id)` で `stopped(stt_id)`、active が解放され、次リクエストが busy 解除後に通ることを追加してください。

9. **should**: `_handle_binary` のサイズ超過は「terminal error」とありますが、`ErrorMsg(id=active_stt_id)` 送信後に buffer/format/auto を全クリアする範囲が曖昧です（`docs/IMPL-S2.md:73-75`）。  
   **修正案**: `_reset_stt_capture()` のような内部 helper を計画に入れ、`active_stt_id/stt_buffer/stt_format/stt_auto_tts` を必ず戻すと明記してください。

10. **nit**: `features.auto_tts=true` にすると既存 `tests/test_ws_tts.py` の S1 ready テストは必ず更新対象です（現状 false 期待）。計画には「既存 pytest 全green」とありますが、既存テスト修正対象が明示されていません。  
   **修正案**: 変更ファイルに `tests/test_ws_tts.py` の ready 期待値更新を追加してください。
hook: Stop
hook: Stop Completed
