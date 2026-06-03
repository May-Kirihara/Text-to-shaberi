# codex レビュー: IMPL-C1（クライアント再生 over WebSocket）

> reviewer: codex-cli 0.135.0 / gpt-5.5 / sandbox=read-only。対象: `docs/IMPL-C1.md` v1。
> 反映先: `docs/IMPL-C1.md` v2（先頭 blockquote に対応番号を記載）。重要度順。

## critical
1. **`PlaybackSink.end()/abort()` の同期 `join()` が qasync ループをブロックする**
   `audio_end` 受信時に `end()` が worker の drain/close 完了まで待つ設計だと、再生キューに残った音声ぶん GUI/WS 受信が止まる。`cancel()` からの `abort()` も同様。
   → end()/abort() は sentinel 投入＋即 return。join は最終 close() だけ `asyncio.to_thread()` に逃がす。`_recv_loop`/Qt callback から同期 join しない。

2. **`cancel` 後の遅延 `audio_end`/binary 処理が計画内で矛盾**
   `_active_id` を `stopped/audio_end/error` まで保持しつつ、cancel 後の `audio_end(id==active)` で `_sink.end()` すると abort 済み sink を drain/close し直す危険。binary も `_cancelling` 中に feed され得る。
   → `_cancelling` 中は binary 破棄、`audio_end(id==active)` は end() せず `_finish_stream()` のみ。`stopped` を正規終端、`error(id==active)` はキャンセル中なら idle 扱い。

3. **worker thread から `on_log=window.log` 直呼びは Qt スレッド違反**
   `PlaybackSink._run()` の生成失敗・write 例外で `window.log`（Qt widget 操作）を worker から呼ぶとクラッシュ/未定義動作。
   → PlaybackSink に event loop を渡し、worker からは `loop.call_soon_threadsafe(on_log, msg)` で main へ戻す。

## should
4. **`player.abort()` を main から直叩きする競合設計が粗い**
   worker が `write_bytes()` 中に main が abort、さらに worker 例外で abort 再入。現行 `_closed` は無ロックで close/abort/write の相互排他なし。
   → `StreamingPlayer` に `threading.Lock` で thread-safe な abort/closed 状態。main は stream abort 要求のみ、close は worker 所有。

5. **`close()` の resampler tail と preroll の順序**
   短発話＋resample 有りで close する場合、tail は buffered preroll の後に出すべき。現実装の tail→preroll は順序逆転。
   → close 時は `preroll` → `resampler last=True tail` の順で連結/書き込み。

6. **`abort(): 常に close()` という表現が危険**
   `close()` が drain sleep/残 preroll write を行う正常終了なら、abort から呼ぶと即停止 SLO と矛盾。
   → abort は `stream.abort()` + `stream.close()` のみ。正常終了 `close()`（drain）と用語分離。

7. **queue 無制限は「非ブロッキング」だけでは不足**
   GPT-SoVITS が実時間より速いと長文で全音声がメモリに積まれる。`ready.limits.max_chunk_bytes` は chunk サイズで総量制限ではない。
   → `queue.Queue(maxsize=N)`。満杯時は abort+cancel/log、または `asyncio.to_thread(q.put)` で WS 側バックプレッシャ。

8. **token クエリ付与は文字列連結では不十分**
   `?`/`&` 判定だけだと token に `&`/`#`/空白を含むと壊れる。
   → `urllib.parse`（urlsplit/urlunsplit/parse_qsl/urlencode）でエンコードして付与。

9. **`speak()` の送信失敗時に busy が残る**
   `_active_id`/busy を立てた後 `_send(Speak)` 失敗で active/busy が残る。
   → `_send` 例外 catch → sink abort・`_finish_stream()`・`on_state("error"/"disconnected")` まで巻き戻す。

10. **`update_client_toml` の文字列エスケープ不足**
    `\`/`"` だけでは newline/control 文字を安全に扱えない。
    → `\n`/`\r`/`\t`/制御文字も TOML basic string としてエスケープ。更新後 `tomllib.loads()` で検証してから `os.replace()`。

11. **行指向 TOML 更新の section 判定を明確化**
    `[server.auth]`・`[[server]]`・コメント `# address =` を誤認すると壊れる。
    → 対象は完全一致の通常 table header `[section]` のみ。次の任意 table header までを範囲。コメント行は key 扱いしない。

## nit
12. **`stream_factory` の注入シグネチャが曖昧**
    `stream_factory()` だけだと samplerate/channels/device/latency を fake 側で検証しにくい。
    → `stream_factory(samplerate=..., channels=..., dtype="float32", device=..., latency="low")` の `sd.OutputStream` 互換 kwargs。

13. **受け入れ基準に qasync/スレッド系テストが不足**
    fake sink 中心で、`PlaybackSink` の worker 終了・abort idempotency・worker ログ marshal・join 非ブロッキングが抜け。
    → fake player で write ブロック中 abort、end() 即 return、abort() 複数回、worker log の main loop 経由を単体テスト追加。
