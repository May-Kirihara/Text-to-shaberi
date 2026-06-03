# X3 codex レビュー指摘（gpt-5.5・read-only・実コード突合）

> 対象: `docs/IMPL-X3.md` v1。総評: **条件付きGO**。「持続 stream + keepalive の方向は妥当だが、初回 warm 保証・abort SLO・worker 状態順序・WSClient の `_sink` ライフサイクル分離 を v2 で直さないと再発・race の可能性が高い」。

## critical
1. **初回 warm 保証**: 「初回 AudioBegin で engine 生成」だと begin 直後に binary が来て、keepalive が device を warm する前に実音声を書く＝先頭欠け再発。→ **`connect()` 成功後に engine を事前起動し、`warm_min_ms` 分の keepalive write 完了を待つ（warm barrier）**。device 変更時も reopen 後に warm barrier、もしくは「変更後初回は頭欠け許容」と仕様明記し受け入れから外す。
2. **abort SLO ≤150ms の根拠**: queue drain だけでは worker が `stream.write(frames)` 実行中は止まらない（大chunk/PipeWire block で 150ms 超あり）。現行 `request_abort()` は別スレッドから `stream.abort()`。→ **実音声 write を 10-20ms に分割し各 write 前後で abort flag を見る（協調 abort）**。SLO 厳守なら `stream.abort()` の fast-abort モード（その後 keepalive 再 warm）も比較検討。
3. **状態所有スレッドの曖昧さ**: `begin` で `_carry=b""`／worker で `_RECONFIG`／abort で queue drain が混在＝back-to-back・AudioEnd 未処理での次 begin・cancel と side-band binary で順序破壊。特に abort の drain が次発話用 `_BEGIN`/`_END` まで落とす危険。→ **`_cur/_resampler/_carry/_stream` は worker だけが触る**。毎発話 **`_BEGIN(seq, fmt, device)` を必ず enqueue**、PCM/END/ABORT にも **seq** を持たせ、worker が**古い seq を破棄**（drain しない）。
4. **WSClient の `_sink` ライフサイクル分離**: `_sink` は「再生中」と「binary 受理可否」のガードを兼ねている。engine を None にしないだけだと `_on_binary` ガード・cancel・error・close/reconnect の意味が壊れる。→ **`_engine`(持続) と `_playback`(発話状態) を分離**。`_finish_stream` は engine を残し `_playback_id/_playback_codec/_opus_decoder` を消す。`_on_binary` は `_engine is not None` でなく **active/playback/cancelling/id** で判定。`close()`/`reconnect()` で engine を閉じるか warm 維持か明文化。
5. **worker 例外復旧**: 「例外で break→close、次 begin で `_ensure_started`」は死んだ thread・`_started/_closing/_stream` の復旧が未定義（次 begin が死んだ worker に enqueue するだけ）。→ **worker 終了時に lock 下で `_thread=None/_stream=None/_started=False/_closing=False` に復旧**。例外後の次 begin 復旧ユニット必須。

## should
1. **keepalive write のブロック**: `get(timeout=20ms)` の最大遅延は「write 即返る」前提。`stream.write(silence)` がブロックすれば実音声/abort も遅延。→ keepalive write のブロック時間をログ化・閾値超で warning。実機検証に「idle 後も sink が RUNNING 相当か（`pactl list sinks`）」を追加。
2. **`_END` の resampler flush**: `resampler.flush()` は現コードと不一致（`StreamingPlayer.close()` は `resample_chunk(empty, last=True)`）。→ `flush_resampler()` helper を切り出し同 API を使う。
3. **発話境界の reset**: source rate 変更時だけでなく**同一 format の次発話でも carry/resampler を発話境界で reset**。END flush 成功前提だと abort 後の次発話で状態漏れ。→ `_BEGIN` で常に carry clear＋resampler 新規生成/明示 reset。
4. **`sink_factory` の意味変更は危険**: 「毎発話生成」→「1回生成」に変えると注入契約が壊れる。→ **`engine_factory` を新設**、`sink_factory` は legacy 残置。engine fake は別途。
5. **device 変更時の prewarm**: 設定で output device 変更時、engine が旧 device を keepalive し続ける／変更後初回が再 warm 対象。→ **`set_device()/prewarm()`** で旧 stream を閉じ新 device を idle prewarm。
6. **検証の客観化**: abort SLO/keepalive が主観寄り。→ **cancel 送信時刻→録音 RMS が閾値未満になるまでの時間**を測る。初回/device変更/idle後で `prime_ms=0` vs `500` を比較（退行判定）。
7. **channels**: `OutputStream(channels=1)` が source channels と切り離し。→ **MVP は mono のみ明記＋拒否ログ**、または channels 変更も reopen 条件に。

## nit
1. sentinel `_CLOSE` 等の型を明記（`_BEGIN/_END/_ABORT/_CLOSE` の dataclass）。
2. fake loop の `call_soon_threadsafe` 呼び出しを明示 assert する engine test。
3. prime の三重状態（client.toml 残置/config 既定500/engine 0推奨）の優先順位を明記。

## 反映方針（v2）
全15点反映。設計の核を以下に確定:
- **seq 方式の単一所有 worker**: 毎発話 `_BEGIN(seq, fmt, device)`、全 item に seq、worker のみが `_cur/_resampler/_carry/_stream` を操作、古い seq は破棄（drain しない）。`_BEGIN` で carry/resampler を必ず reset。
- **eager prewarm + warm barrier**: connect 後に engine 起動＋keepalive、`warm_min_ms` 待ってから「warm 済み」。初回頭欠けゼロを保証。
- **協調 abort**: 実音声 write を ≤20ms 分割し write 間で abort(seq変更) を見る → ≤~20ms+device latency で停止。stream は閉じない（warm 維持）。
- **WSClient `_engine`/`_playback` 分離**: engine は connect で生成し close まで持続（reconnect では維持）。`_finish_stream` は playback 状態のみ消す。`_on_binary` は active/playback/cancelling/id で判定。
- **worker 例外復旧**・**engine_factory 新設**・**device 変更 prewarm**・**flush_resampler helper**・**mono明記**・**検証は cancel→無音時間を計測**。
