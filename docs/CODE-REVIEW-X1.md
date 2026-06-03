# X1 コードレビュー / 検証記録

> 対象: X1「Opus コーデック ＋ セキュリティ（token/wss/TLS）」（GLM-5.1 実装 → 私が検証・統合・修正）。
> 仕様: `docs/IMPL-X1.md` v2（codexレビュー `docs/REVIEW-codex-X1.md` の16点反映済み）。

## 1. 実装サマリ（GLM）
| ファイル | 内容 | 判定 |
|---|---|---|
| `common/opus_codec.py` | `OPUS_*` 定数・`opus_wire_format()`・`opus_available()`・`OpusStreamEncoder`（入力carry→float32 soxr→clip→<i2→960枠encode）・`OpusStreamDecoder`（960固定）。encoder/decoder/resampler factory 分離・遅延import・`APPLICATION_AUDIO` | ✅（flush を1点修正） |
| `server/orchestrator.py` | `_handshake` で session 単位コーデック採用＋`Ready.codec`/`Ready.limits["opus"]`＋fallback warning・`Session.codec`・`_stream_tts` opus 分岐＋`_to_mono_int16` downmix | ✅ 仕様準拠 |
| `server/ws_server.py` | token: **Bearer 優先→query fallback**・`hmac.compare_digest`・不一致 close(1008) | ✅ |
| `server/config.py` | `BindConfig`+tls_cert/tls_key・`TransportConfig`(codec="opus"/opus_bitrate)・`validate_tls`・`normalize_transport_codec` | ✅ |
| `server/app.py` | `validate_tls` 後 TLS 時 uvicorn に ssl 引数・opus_available 表示 | ✅ |
| `client/ws_client.py` | caps に opus・**Bearer ヘッダ既定**＋token_in_query・**wss ssl context**(cafile/insecure)・AudioBegin opus で**sink へ PCM fmt 分離**・`_on_binary` opus decode＋例外 terminal・`_playback_codec`/`_opus_decoder` リセット | ✅ 仕様準拠 |
| `client/config.py`/`client.toml` | `ServerConfig`+codec/tls_insecure/tls_cafile/token_in_query | ✅ |
| `server.toml` | `[bind]` tls_*・`[transport]` codec="opus"/opus_bitrate | ✅ |
| `docs/SETUP.md` | 自己署名cert・Windows libopus DLL・リバースプロキシ注記 | ✅ |
| `tests/test_opus_codec.py`(16)・`tests/test_ws_negotiation.py`(22)・`tests/test_ws_client.py`(+opus)・`tests/test_import_boundaries.py` | 実opuslib往復＋fake境界・ネゴシエーション・token(TestClient.websocket_connect)・validate_tls・normalize・ssl引数 | ✅ |

codex 16指摘（critical4/should8/nit4）反映を確認: ①opus時sinkはPCM fmt・②session固定+mono downmix(Ready.codec==全AudioBegin.codec)・③float32リサンプル・④Bearer既定+TLS無し警告・⑤flush drain→padding・⑥入力byte carry・⑦decode例外terminal・⑧opus定数一元化+limits必須・⑨fallback可視化・⑩validate_tls・⑪Windows DLL+プローブ・⑫token TestClient・⑬factory分割・⑭APPLICATION_AUDIO・⑮normalize_transport_codec・⑯帯域数値化。

## 2. 私による修正
- **`common/opus_codec.py` `flush()` の負パディング・エッジ**: GLM 実装は `padded = self._buf + b"\x00"*(frame_bytes - len(self._buf))` で、drain により `self._buf` が 1 フレーム(1920B)超になると **負パディング＝padしないまま `encode(>1920B, 960)`** となり、実 opuslib では frame_size 不整合になり得た。→ **「drain → 完全フレームを先に全部 encode → 残り端数のみ無音pad」** に堅牢化（`encode` には常に正確に 960 サンプルを渡す）。奇数バイト feed＋端数入力での往復実測で全パケット 960 サンプル・総数 期待±960 を確認。
- 付随して、この挙動をバグ実装に合わせていた `test_flush_drains_resampler`（`len(pkts)==1` 期待）を**正しい挙動（1440サンプル→full+padded=2パケット）に訂正**。

## 3. 検証結果
- **ユニット**: `uv run pytest -q` **159 passed**（C2の115 + opus16 + negotiation22 + ws_client opus + 他）。複数回クリーン・OOM/ハング無し。
- **import境界**: `opuslib`/`soxr` はトップレベル import せず（`common/opus_codec.py` も factory/関数内遅延）。`tests/test_import_boundaries.py` green。
- **opus 往復（実opuslib＋実soxr）**: 32k/16k/48k・奇数バイト feed で各パケット 960 サンプル・総数 期待±960・flush drain/padding 分離 を確認。
- **実サーバ opus end-to-end（実 GPT-SoVITS）** `/tmp/x1_itest.py`:
  - Part A（実 WSClient + RecordingSink）: `ready.codec="opus"`・`limits.opus={available,48000,20,24000}`・**`sink.begin codec="pcm_s16le"/48000`（wire↔playback 分離）**・decoded PCM 232KB/121feeds・lifecycle ready→connected→busy→idle。
  - Part B（生 ws・wire 実測）: `audio_begin codec="opus"/48000/1`・opus packets=119・**wire 6506B で 2.38秒**・PCM相当228KB → **圧縮比≈35x**・**TTFA 132ms**。RESULT PASS。
- **wss/TLS スモーク（自己署名・Qwen不要 fake 注入）** `/tmp/x1_tls_smoke.py`:
  - wss + Bearer + CA(tls_cafile) → connect 成功（pcm 優先指定だったので fallback 警告も可視化）。
  - wss + tls_insecure → 成功。wss + 誤token → **拒否(1008)**。RESULT PASS。

## 4. 既知の小課題・後続へ
- **多ch TTS の per-chunk downmix（`_to_mono_int16`）**は、チャンクが多chフレーム境界で割り切れない場合に端数を捨てる潜在ズレあり。**MVP の GPT-SoVITS は 32k mono なので未発火**。多ch profile を使うなら downmix にも byte carry を入れる（X2 候補）。
- **opus decode はイベントループ上（`_on_binary`）**: 実測 TTFA 132ms で問題なし。重い環境では将来 sink ワーカーへ移譲。
- **マイクアップリンクは PCM 16k 据え置き**（スコープ外）。Opus uplink は将来（16k ネイティブで容易）。
- **Windows 実機**（あなた）: opuslib の libopus DLL 配置（`docs/SETUP.md`）、`wss://`＋opus 再生、Bearer token、自己署名CA の確認が残課題（headless では実マイク/Windows DLL 不可）。
