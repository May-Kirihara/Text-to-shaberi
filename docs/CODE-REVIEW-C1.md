# C1 コードレビュー / 検証記録

> 対象: C1「クライアント再生 over WebSocket」（GLM-5.1 実装 → 私が検証・統合）。
> 仕様: `docs/IMPL-C1.md` v2（codexレビュー `docs/REVIEW-codex-C1.md` 反映済み）。

## 1. 実装サマリ（GLM）
| ファイル | 内容 | 判定 |
|---|---|---|
| `client/ws_client.py` | WSClient 本実装（hello/ready・speak/cancel・受信ディスパッチ・cancel状態規則・urllib token・送信失敗巻き戻し・close で join を to_thread） | ✅ 仕様準拠 |
| `client/audio/sink.py` | PlaybackSink（daemon worker・sentinel・非ブロッキング feed/end/abort・有界queue・worker ログは call_soon_threadsafe・join timeout） | ✅ 仕様準拠 |
| `client/audio/player.py` | StreamingPlayer 遅延スタート＋threading.Lock＋request_abort/close 分離＋close は preroll→tail 順＋factory注入 | ✅ 仕様準拠 |
| `client/settings_store.py` | update_client_toml（厳密 section 判定・制御文字エスケープ・tomllib検証・原子的書き込み） | ✅ 仕様準拠 |
| `client/ui/settings_dialog.py` | 歯車設定（アドレス/トークン(Password)/プロファイル） | ✅ |
| `client/ui/main_window.py` | 歯車「⚙」ボタン＋接続ラベル | ✅ |
| `client/app.py` | WSClient/PlaybackSink 配線・startup接続・on_settings→保存→再接続 | ✅ |
| `tests/test_{player_lazy_start,playback_sink,ws_client,settings_store}.py` | ユニット4本 | ✅ 65 passed |

codex 13指摘（critical×3/should×8/nit×2）はすべて実装に反映されていることを確認。

## 2. 私による修正
- **`speak()` のプロファイル送信欠落（spec §3.4「profile or cfg値 or None」のデッドコード化）**: GLM 実装は `if profile_val is None ...: pass` で `cfg.profile.active_id` を送っていなかった。`WSClient(default_profile=...)` を追加し `profile or self._default_profile or None` に修正。`app.py` で構築時に `cfg.profile.active_id` を渡し、設定保存時に `ws._default_profile` を更新。`tests/test_ws_client.py::test_default_profile_sent` を追加（既定送信＋明示上書き）。

## 3. 検証結果
- **ユニット**: `pytest` **65 passed**（既存含む）。13回以上の連続フルラン安定。
- **import境界**: client→server/torch/qwen/silero 禁止・sounddevice/soxr 遅延import を AST テストで green。
- **実サーバ end-to-end（Linux, 私）**: `text-to-shaberi-server`(:8765)＋実 GPT-SoVITS(:9880)＋実デバイス(PipeWire)で
  - connect→ready(profiles=['gpt-sovits'], 32000/mono)→speak→audio_begin→binary→audio_end→idle のクリーンな遷移。
  - **TTFA(send→first-chunk)=145ms**（S1 実測148msと同等＝クライアント経由の overhead 無し）。bytes=529908 / dur=8.28s。
  - 実 PlaybackSink/StreamingPlayer の**遅延スタートを実機経路で通過・クラッシュ無し**。

## 4. 「間欠ハング」調査メモ
- 委託直後の最初の `uv run pytest` が約7分ハングした。原因を切り分け:
  - 全テストファイルを個別 timeout 実行 → 全 pass・ハング無し。
  - GLM 完了後にフルスイートを13回以上連続実行 → 全 65 passed・ハング **0回**。
- 結論: **コードの欠陥ではなく、GLM の opencode が背後で自分の `uv run pytest` を並走させ、uv の環境ロックを奪い合った競合**（GLM は MCP の ReadTimeout 後もバックグラウンドで作業継続＝既知パターン）。GLM 完了後は完全に安定。

## 5. 未対応・後続マイルストーンへ送る項目
- **可聴確認（先頭欠け無し）**: ヘッドレスでは音が鳴ることまで確認。**「先頭が欠けない」可聴確認はあなたの環境で**（lazy-start は構造的に修正済み＋ユニットで担保）。
- **Windows実機**: WASAPI 既定出力・VB-CABLE を combo 選択・停止・先頭欠け無し → あなたの担当。
- **TTS_Virtual ルーティング配線**: combo の `__ttsvirt__` は当面 device=None（ログ表示のみ）。`audio/routing.py` の結線は後続（X2 想定）。
- **本格バックプレッシャ**: 現状は有界queue満杯で abort+cancel。WS側 await による真のバックプレッシャは C2。
- **再接続バックオフ/全設定項目**: X2。
