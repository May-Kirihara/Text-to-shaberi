# S2 コードレビュー / 検証記録

> 対象: S2「サーバ STT over WebSocket（+ auto_tts）」（GLM-5.1 実装 → 私が検証・統合）。
> 仕様: `docs/IMPL-S2.md` v2（codexレビュー `docs/REVIEW-codex-S2.md` 反映済み）。

## 1. 実装サマリ（GLM）
| ファイル | 内容 | 判定 |
|---|---|---|
| `server/stt/engine.py` | `STTEngine`(Protocol)/`QwenSTTEngine`（遅延import・dtypeマップ・`_LANG_MAP`・`transcribe(audio=(samples,sr))`） | ✅ 仕様準拠 |
| `server/stt/__init__.py` | re-export | ✅ |
| `server/ws_server.py` | `create_app(...,stt_engine=None)`・**単一ワーカー executor**(`app.state.stt_executor`)・lifespan で owned STT(backend判定/RuntimeError)・SessionState 拡張 | ✅ 仕様準拠 |
| `server/orchestrator.py` | `BinaryFrame`・STT状態・`_busy`/`_reset_stt_capture`・stt_begin/binary/stt_end・`_process_stt`(executor)・`_run_auto_tts`・`_stream_tts(parent_id,manage_active)`・cancel三者照合・`features.auto_tts=true` | ✅ 仕様準拠 |
| `tests/test_ws_stt.py` | 19テスト（FakeSTTEngine/FakeTTSClient 注入） | ✅ |
| `tests/test_ws_tts.py` | ready の `features.auto_tts` 期待値を True に更新 | ✅ |

codex 10指摘（critical×2/should×7/nit×1）はすべて実装に反映済みを確認（executor直列化・manage_active・cancel stopped.id・PCM端数切詰め・stt_end side-band・秒数上限・backend判定・STT推論中cancel・reset helper・test_ws_tts更新）。

## 2. 私による修正
- **auto_tts の TTS 中 cancel で `stopped` 二重送信**（`_stream_tts` が `stopped(tts_id)` 送出→re-raise→`_process_stt` の except でも再送）。`_process_stt` の CancelledError ハンドラに `if self.active_parent_id is None:` ガードを追加し、auto_tts TTS 中は `_stream_tts` 側の1回のみに統一（STT推論中の cancel は従来通り `_process_stt` が `stopped(stt_id)` を送る）。

## 3. 検証結果
- **ユニット**: `pytest` **84 passed**（既存56 + STT19 + test_ws_tts更新含む）。複数回連続クリーン・OOM/ハング無し。
- **import境界**: server→PySide6/sounddevice 禁止・torch/qwen_asr は関数内遅延import を AST テストで green。
- **実サーバ end-to-end（Linux, 実 Qwen3-ASR）**: `text-to-shaberi-server`(:8765, owned STT) ＋ 実 GPT-SoVITS で
  - `ready.features.auto_tts==True`。
  - ref.wav(48k mono)→16k 化→`stt_begin(auto_tts=true)`→binary→`stt_end` → **`transcript`(1.21s)** = 「パックマンがある条件を満たすと、追ってくるモンスターを逆襲して食べることができる。」（正確な日本語認識）。
  - auto_tts 連結 = `audio_begin{id="tts-u2", parent_id="u2"}`→binary→`audio_end`（音声 6.58s、**transcript→初音 119ms**）。
  - Qwen ロード時 GPU ~4.7GB増（11991MiB）、停止で解放。

## 4. 調査メモ（OOM の誤検知）
- 委託直後の最初の `uv run pytest` で 6 失敗（CUDA OOM, "Loading checkpoint shards"）。原因切り分け:
  - 各テストファイルを個別実行 → 全 pass・GPU増加 0・OOM 0（全テストが fake 注入で実 Qwen を読まない）。
  - GLM 完了後にフルスイート再実行 → 84 passed・GPU 不変・OOM 0（複数回再現）。
- 結論: **コード欠陥ではなく、GLM の opencode が完了処理で自分の `uv run`（実Qwen含む）を並走させ VRAM/uv ロックを奪っていた**ための一過性。GLM のファイル編集が静止してから検証すべき（C1 の uv ロック競合と同根）。閾値75sの静止監視が GLM の長い推論ポーズで早期判定したため、120s に延ばして再確認した。

## 5. 未対応・後続へ
- **クライアント側マイク/VAD/送信**（C2）: 端末マイク常時録音・webrtcvad・発話セグメント→`stt_begin`/binary/`stt_end`→`transcript`/auto_tts 音声・最低限バックプレッシャ。
- 部分/逐次 transcript（`final=false`）は未対応（MVP は発話単位 `final=true` のみ）。
- STT バックエンド差替（DashScope クラウド）は将来（`backend` 抽象は用意済み）。
