# X1 codex レビュー指摘（gpt-5.5・read-only・実コード突合）

> 対象: `docs/IMPL-X1.md` v1。総評: **条件付きGO**（critical×4 / should×8 / nit×4）。
> 「Opus 方針自体は妥当。`AudioBegin(opus)` と PCM sink 契約・per-stream fallback・token の query 既定 を v2 で直してから実装」。

## critical
1. **§3.4/ws_client/sink — opus 時 sink へ PCM フォーマットを渡す**: `AudioBegin.format.codec="opus"` の fmt をそのまま `sink.begin(fmt)` に渡すと `PlaybackSink`→`StreamingPlayer` が `encoding="opus"` を受け `bytes_per_sample("opus")` で落ちる。→ **wire format と playback PCM format を分離**。opus 時は `_playback_codec="opus"`＋decoder を保持しつつ `sink.begin(AudioFormatPayload(codec="pcm_s16le", sample_rate=48000, channels=1), device)` を渡す。テストで fake sink の begin codec が `pcm_s16le` を検証。
2. **§3.2/§3.3 — session.codec と per-stream の不整合**: 接続単位 `Ready.codec` 固定なのに「多ch profile は pcm フォールバック」は per-stream。speak ごとに profile を選べるため矛盾。→ **MVP は session 単位コーデック固定で一貫**させ、opus は **mono 固定**＝多ch TTS は **mono ダウンミックスしてから encode**（per-stream fallback しない）。`Ready.codec == 全 AudioBegin.format.codec` を不変条件に。
3. **§3.1 — soxr int16 ストリーミング未検証**: 既存は float32。→ **float32 経路に固定**: `_pcm_to_float32 → soxr.ResampleStream(dtype="float32") → clip → <i2`。受け入れに「1秒32k入力→decode後サンプル ~48000（+flush padding ≤960）」「各 Opus packet decode が常に 960 サンプル」を入れる。【de-risk 実測済み: float32/int16 とも 1s→48000 だが float32 採用】。
4. **§3.5/ws_server — token を query 既定にしない**: query token はアクセス/プロキシログに残る。`compare_digest` 化だけでは甘い。→ **推奨経路を `Authorization: Bearer` に**。client は token を **header 送信を既定**、query は legacy fallback。TLS 無し外部 bind は token 必須＋**明示警告**。

## should
5. **§3.1 — flush の drain と padding を分離**: resampler を `resample_chunk(empty, last=True)` で drain してから無音パディング。テストは feed 完成フレーム数 / flush 0|1 packet / 総 decoded サンプルを**別々に**検証。
6. **§3.1/§3.3 — 入力32k のバイト境界 carry**: TTS `stream()` の HTTP chunk は sample 境界に揃わない。encoder に `_input_carry` を持ち `bytes_per_sample("pcm_s16le")*channels` 境界で切ってから resampler へ。
7. **§3.4 — opus decode 例外の仕様化**: `_on_binary` の decode 例外は terminal: `sink.abort()`→`_finish_stream()`→`_state("error",...)`（必要なら `Cancel(_active_id)`）。ログだけにしない。
8. **§3.2 — opus wire 定数の一元化**: `"opus"` 裸文字列・48k/20ms/mono 暗黙契約は将来事故。→ `opus_codec.py` に `OPUS_CODEC_NAME`/`OPUS_WIRE_FORMAT`(helper) を置き双方共有。`Ready.limits["opus"]` は**必ず出す**、client は不一致なら pcm 降格 or error。
9. **§3.2/§3.6 — silent fallback の可視化**: 既定 opus で opuslib/libopus 不在時に黙って pcm 降格すると運用者が誤認。→ fallback 理由を **server log / `Ready.limits` に出す**。`codec="opus"` で import 失敗なら warning。
10. **§3.5/app — TLS cert/key 片方指定の扱い未定義**: → `validate_tls(bind)` を追加（cert/key 両方必須・存在確認）。`tls_insecure` は client 側のみ。テスト追加。
11. **§7 — Windows の opuslib DLL**: `libopus.so.0` 前提は Linux 限定。Windows は DLL 探索/同梱が別。→ SETUP に Windows DLL 配置手順、client `--check`/起動前に `import opuslib; Encoder(48000,1,...)` プローブを入れる。
12. **§4/§6 — token テストを TestClient.websocket_connect で**: 正token(query)/正token(Authorization)/誤token/空token(accept) を全部見る。可能なら compare_digest 呼出も確認。

## nit
13. **§3.1** — `opus_factory` を `encoder_factory(rate,ch,app)` / `decoder_factory(rate,ch)` に分割（fake が書きやすい）。
14. **§3.1** — TTS 音声の自然さなら `APPLICATION_AUDIO` を第一候補（VOIP は会話低遅延向け）。
15. **§3.6** — 設定名 `"pcm"`/`"opus"` と wire 名 `"pcm_s16le"`/`"opus"` の混在 → `normalize_transport_codec()` で変換を一箇所に。
16. **§4** — 帯域基準を数値化（1秒32k mono PCM=64000B、24kbps Opus は payload 概ね 3000〜8000B/s）。

## 反映方針（v2）
全16点反映。critical 4点（opus時sinkはPCM fmt・session固定+mono downmix・float32リサンプル・Bearer既定）と should#8/#9（定数一元化・fallback可視化）はコード挙動直結で明文化。
