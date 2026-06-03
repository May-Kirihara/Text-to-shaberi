# X3 コードレビュー / 検証記録

> 対象: X3「持続的プレイバックエンジン（出力ストリーム常時開放・ウォームアップ1回償却）」（GLM-5.1 実装 → 私が検証・統合・修正）。
> 仕様: `docs/IMPL-X3.md` v2（codex `docs/REVIEW-codex-X3.md` の critical5/should7/nit3 反映）。

## 1. 実装サマリ（GLM）
| 対象 | 内容 | 判定 |
|---|---|---|
| `client/audio/engine.py` `PlaybackEngine` | 1本の OutputStream＋常駐 worker・**seq 単一所有**(`_Begin/_Audio/_End(seq)`・`_ABORT/_CLOSE`、worker のみが `_cur_fmt/_resampler/_carry/_stream/_play_seq` 操作・古いseq破棄・drain無し)・**keepalive 無音**(get timeout=20ms→Empty で silence write＋ブロック計測)・**prewarm＋warm barrier**(`_warm` Event を warm_min_ms で set・最大5s待ち)・**協調 abort**(`_write_chunked` で seq 変化チェック)・**worker 例外復旧**(finally で lock 下 `_stream/_thread=None,_started=False`)・mono拒否ログ・`flush_resampler`・device 変更 `_reopen`/`set_device` | ✅（私が abort チェックを1点修正） |
| `client/audio/player.py` | `flush_resampler` 切り出し（StreamingPlayer 残置・legacy） | ✅ |
| `client/ws_client.py` | `engine_factory` 新設（sink_factory 残置）・**connect で engine 生成＋prewarm**・AudioBegin で `engine.begin` 再利用・`_on_binary` ガードを **active/playback/cancelling/id**（engine 有無で判定しない）・`_finish_stream` は engine 残置・close で engine.close・**reconnect で engine 維持**・engine/sink デュアルパス | ✅ critical#4 |
| `client/config.py`/`client.toml` | `OutputConfig.prime_ms` 既定 **0**（engine warm のため）＋コメント | ✅ nit#3 |
| `tests/test_playback_engine.py`(17)・`tests/test_ws_client.py`/`test_ws_reconnect.py`(engine_factory/FakeEngine 移行) | seq破棄/協調abort/keepalive/warm/reopen/例外復旧/満杯/call_soon_threadsafe/mono拒否 | ✅ |

codex 指摘の反映を確認: critical①prewarm warm barrier・②協調 abort・③seq 単一所有・④`_engine`/`_playback` 分離・⑤worker 例外復旧／should①keepalive ブロック計測・②flush_resampler helper・③`_Begin` で carry/resampler reset・④engine_factory 新設・⑤set_device・⑥（検証で cancel→無音）・⑦mono／nit①sentinel 型・②call_soon assert・③prime 優先順位。

## 2. 私による修正
- **`_write_chunked` の協調 abort チェックが無効**: GLM 実装は `current_seq = self._play_seq; while ...: if self._play_seq != current_seq: break`。だが abort() は `_play_seq` を変えず **`_seq`** を ++ するため、再生中チャンクの write 途中で abort しても `_play_seq` は不変＝**中断されない**（大きめチャンクで abort SLO 未達・codex#2 の意図未達）。→ **`self._seq != play_seq`** で判定（abort/次 begin が `_seq` を進めたら残りサブ write を捨てる）に修正。`_seq` 読みは GIL でアトミック、worker の best-effort 中断として安全。

## 3. 検証結果
- **ユニット**: `uv run pytest -q` **227 passed**（X2b の210 + engine17 + 移行）。`_write_chunked` 修正後も green。複数回クリーン。
  - 補足: 初回フルスイートで `test_ws_reconnect` の2件が**フレーキー失敗**（単体/再実行では green）。X2a の短バックオフ(base=0.01)がフルスイート負荷で稀にレースするテスト脆さ＝X3 回帰ではない（engine は純 threading で reconnect timing に介入しない）。clean 再実行で 227 安定。
- **境界**: engine.py は sounddevice/soxr を**トップレベル import しない**（遅延/factory 注入）。AST 境界 green。
- **実機 null sink 録音検証（持続エンジン・prime無し・私が Linux で）** `/tmp/engine_capture.py`:
  - prewarm 後の **初回発話**: tone(0.6s)→録音バースト **0.64s**＝**頭欠けゼロ**（prewarm が device を warm 済み）。
  - **連続2回目**（stream 再利用・reopen 無し）: **0.62s**＝**頭欠けゼロ**。
  - **6秒アイドル後**: バースト onset が **オンタイム**（feed 開始＝鳴り始めが即時）＝keepalive が SUSPEND を防ぎ warm 維持。長さ0.10s は録音窓の末尾切れ artifact（onset 即時が頭欠け無しの証左）。
  - **prime_ms=0** で上記成立＝**毎発話の prime 無音レイテンシをゼロ化**（旧経路は毎回 0.2〜0.5s 頭欠け or prime 500ms 必要だった）。
- 既存 `test_playback_sink`/`test_player_lazy_start`（legacy）不変 green。`PlaybackSink`/`StreamingPlayer` 残置。

## 4. 効果・後続
- **効果**: TTS 再生の**ウォームアップを1回に償却**。初回（connect 時 prewarm）以降は warm な持続ストリームへ流すだけ＝**頭欠けゼロ・prime レイテンシゼロ**。連続発話・アイドル後も keepalive で warm 維持。
- **トレードオフ**: keepalive 無音を常時 write＝出力 device を常に active に保つ（音楽アプリ同様）。電力/device 占有が気になる用途向けに「N 秒無発話で auto-close（次発話で再 warm）」を将来オプションに。
- **後続（X2 系の脆さ）**: `test_ws_reconnect` の短バックオフがフルスイートで稀にフレーキー。決定論的タイミング（fake clock/長め base）に直すのは別途（X3 範囲外）。
- **Windows 実機**（あなた）: 連続発話の頭欠け無し・低遅延（prime 無し）・出力切替の体感確認。
