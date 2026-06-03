# X1 実装計画書 v2: Opus コーデック ＋ セキュリティ（token/wss/TLS）

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2)。S1/C1/S2/C2 で「テキスト/マイク→STT→auto_tts→再生」の縦疎通は完成。X1 は **Windows↔Linux の WS 回線を本番化** する: ①TTS ダウンリンクを **Opus 圧縮**（帯域 ~1/9）・②**token 認証の堅牢化**＋**wss/TLS** で外部接続を安全化。
> フロー: 本書 → codexレビュー(`docs/REVIEW-codex-X1.md`) → **v2改訂** → GLM委託 → 私が検証/統合 → `docs/CODE-REVIEW-X1.md`。**この仕様の通りに実装すること。**
> **v2: codexレビュー(`docs/REVIEW-codex-X1.md`)反映** = ①opus 時 sink へは **PCM フォーマット**(pcm_s16le/48000/1)を渡す(wire と playback を分離)・②**session 単位コーデック固定**＋opus は **mono ダウンミックス**(per-stream fallback しない、Ready.codec==全AudioBegin.codec)・③リサンプルは **float32 経路**(`_pcm_to_float32→ResampleStream(float32)→clip→<i2`)・④token は **`Authorization: Bearer` 既定**(query は legacy fallback)＋TLS無し外部bindで警告・⑤flush は **drain→padding 分離**・⑥encoder に **入力32k のバイト境界 carry**・⑦opus decode 例外は **terminal 仕様化**・⑧**opus wire 定数を一元化**(OPUS_CODEC_NAME/OPUS_WIRE_FORMAT)＋`Ready.limits["opus"]`必須・⑨**fallback 可視化**(log/limits)・⑩**`validate_tls`**(cert/key 両方必須・存在確認)・⑪Windows DLL 手順＋opuslib 起動プローブ・⑫token テストは TestClient.websocket_connect・⑬encoder_factory/decoder_factory 分割・⑭`APPLICATION_AUDIO` 既定・⑮`normalize_transport_codec()`・⑯帯域基準を数値化。
> 前提: C2完了（`d1d392a`）。`text-to-shaberi-server`(:8765, owned STT) ＋ 実 GPT-SoVITS が稼働。
> **新規依存**: `opuslib>=3.0.1`（client/server core dependencies に追加（common/opus_codec は client/server 双方が import する共有モジュールのため。extra ではなく `[project.dependencies]` ＝`uv run` で常時導入）。**ctypes で system `libopus.so.0` を呼ぶ**＝Linux 当環境に存在確認済み。Windows は §7 の DLL 配置手順）。`soxr`（既存・両extra）でリサンプル。**de-risk 実測済み**: opuslib 16k mono 20ms 往復 OK（640B→Opus69B→640B）、PyAV で Opus 対応レート=`{8000,12000,16000,24000,48000}`＝**32000非対応**を確認、soxr float32 ストリーミング 32k→48k で 1秒→48000 サンプルを確認。

## 1. 目的・スコープ
- スコープ内:
  - **Opus ダウンリンク**（サーバ TTS → クライアント再生）: サーバが TTS PCM(32k) を **soxr で 48k へ→20ms フレーム→opus エンコード**しバイナリ送出。クライアントは **opus デコード→48k PCM→既存 `StreamingPlayer`（48k→出力デバイス soxr）** で再生。**1 WS バイナリフレーム = 1 Opus パケット**（決定論的）。
  - **コーデックネゴシエーション**: `Hello.caps.codecs` と `server.toml [transport].codec` から **採用コーデックを決定**し `Ready.codec` で通知、各 `AudioBegin.format.codec` で per-stream 明示。**非対応クライアント/設定は `pcm_s16le` にフォールバック**（C1/C2 既存クライアントは無改修で動く）。
  - **共有コーデック層** `common/opus_codec.py`（**新規** `OpusStreamEncoder`/`OpusStreamDecoder`。opuslib/soxr は遅延import）。
  - **セキュリティ**:
    - **token 認証の堅牢化**: 既存の query/`Bearer` token 検証（`ws_server.py`）を **`hmac.compare_digest`（定数時間比較）** に。`validate_bind`（非loopback は token 必須）は起動時呼出し済み（`app.py`）。
    - **wss/TLS**: `server.toml [bind].tls_cert`/`tls_key` 設定時に **uvicorn を TLS 起動**。クライアントは `wss://...` で接続。自己署名向けに client `[server].tls_insecure`（検証無効）/`tls_cafile`（カスタムCA）を用意。
  - 設定拡張・ユニットテスト（opus 往復/ネゴシエーション/フォールバック/compare_digest）。
- スコープ外（明示）:
  - **Opus アップリンク（マイク→STT）は対象外**＝**PCM 16k のまま**。理由: (a) 16k mono PCM は 32KB/s と既に小さい、(b) Opus は非可逆で STT 精度に影響し得る、(c) MVP の面を絞る。将来オプション（16k は Opus ネイティブ対応なのでリサンプル不要・容易）。スコープ外と明記。
  - 可変ビットレート/DTX/FEC/PLC（WS=TCP で順序保証・ロス無しのため不要）。Opus 以外のコーデック。再接続/全設定UI（X2）。
  - **TLS 終端をリバースプロキシに委ねる運用**も可だが、本書は **uvicorn ネイティブ TLS** を一次サポート（プロキシ運用は SETUP に注記）。

## 2. 既存資産
- `common/protocol.py`: `Hello.caps`(dict)・`Ready.codec`/`Ready.limits`/`Ready.features`・`AudioFormatPayload(codec, sample_rate, channels)`・`AudioBegin.format`。**X1 は AudioFormatPayload に追加フィールドを作らず、opus は固定定数（48000Hz/20ms）で運用**（後述§3.2）。
- `common/pcm.py`: `_pcm_to_float32`/`bytes_per_sample`/`split_full_samples`。
- `common/audio_format.py`: `CodecFormat`（「X1 Opus 用の器」プレースホルダ・流用可）。
- `server/ws_server.py`: `/ws` で token 検証（query/`Bearer`、不一致は close(1008)）。**compare_digest 化のみ**。
- `server/config.py`: `BindConfig(host/port/token)`・`validate_bind`（非loopback+token空で ValueError）・`TransportConfig(codec="pcm")`。
- `server/app.py`: `run_server` が `validate_bind` 後 `uvicorn.run(app, host, port)`。**TLS 引数追加**。
- `server/orchestrator.py`: `_stream_tts(stream_id, profile, text, parent_id, manage_active)` が `tts_client.stream()` の PCM チャンクを `_send_bytes` で送出、`AudioBegin(format=...)` を送る。**ここに opus エンコードを差し込む**。`_handshake` が `Ready` を組む。**ここで採用コーデック決定**。
- `server/tts/client.py`: `stream(profile, text)` が PCM bytes を逐次 yield（profile.audio = 32000/mono 既定）。
- `client/ws_client.py`: `connect` で `Hello(caps={"codecs":["pcm_s16le"]})`・`_on_binary` が PCM を `sink.feed`・`_on_message` の `AudioBegin` で `sink.begin(fmt, device)`。**caps に opus 追加・binary 経路に opus デコード・TLS ssl 注入**。
- `client/audio/sink.py`/`player.py`: `PlaybackSink`/`StreamingPlayer`（src_rate→device soxr）。**不変**（opus は ws_client 側でデコードして PCM を流すので sink/player は PCM のまま）。
- 依存: 既存 client/server extra に `soxr`/`websockets`/`numpy`。**`opuslib>=3.0.1` をcore dependencies に追加（common/opus_codec は client/server 双方が import する共有モジュールのため。extra ではなく `[project.dependencies]` ＝`uv run` で常時導入）**。

## 3. 設計

### 3.1 `common/opus_codec.py`（新規・共有）
opuslib/soxr は**遅延import/注入**（テスト容易・pcm専用環境で読まない）。
```python
from .protocol import AudioFormatPayload

OPUS_CODEC_NAME = "opus"      # wire 上のコーデック名（codex#8: 裸文字列を一元化）
OPUS_RATE = 48000             # Opus 内部レート（48k 固定）
OPUS_FRAME_MS = 20            # フレーム長（20ms 固定）。frame_size = 48000*20/1000 = 960
OPUS_FRAME_SIZE = 960         # サンプル/フレーム（mono）
OPUS_CHANNELS = 1             # mono 固定（MVP）

def opus_wire_format() -> AudioFormatPayload:   # codec="opus", sample_rate=48000, channels=1
    return AudioFormatPayload(codec=OPUS_CODEC_NAME, sample_rate=OPUS_RATE, channels=OPUS_CHANNELS)

def opus_available() -> bool:    # opuslib が import でき Encoder 生成可能か（起動プローブにも流用・codex#9/#11）
    try:
        import opuslib
        opuslib.Encoder(OPUS_RATE, OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
        return True
    except Exception:
        return False

class OpusStreamEncoder:
    def __init__(self, src_rate, bitrate=24000, *, encoder_factory=None, resampler_factory=None): ...
    def feed(self, pcm_s16le_mono: bytes) -> list[bytes]:   # 完成した Opus パケット(0+)
    def flush(self) -> list[bytes]:                         # drain→端数を無音pad→最後のパケット(0|1)

class OpusStreamDecoder:
    def __init__(self, *, decoder_factory=None): ...
    def decode(self, packet: bytes) -> bytes:               # opus packet → 48k mono int16 PCM bytes(=960samples)
```
- **mono 固定（MVP）**: encoder/decoder とも **mono のみ**。多ch は呼び出し側（`_stream_tts`）が mono ダウンミックスしてから渡す（§3.3・codex#2）。
- **エンコーダ生成（codex#13,#14）**: `encoder_factory` 既定 = `lambda: (e:=opuslib.Encoder(OPUS_RATE, 1, opuslib.APPLICATION_AUDIO), e.bitrate=bitrate, e)[-1]` 相当。**`APPLICATION_AUDIO` を既定**（TTS 音声の自然さ重視。低遅延会話は将来設定化）。
- **`feed`（順序厳守・codex#3,#6）**:
  1. **入力バイト境界 carry**: `full, self._input_carry = split_full_samples(self._input_carry + pcm, bytes_per_sample("pcm_s16le")*1)` で **完全サンプル境界**に揃える（TTS の HTTP chunk は sample 境界に揃わない）。
  2. **float32 リサンプル（既存 player と同方式・codex#3）**: `frames = _pcm_to_float32(full, "pcm_s16le", 1)`（mono 1D）→ `src_rate != 48000` なら `self._rs.resample_chunk(frames)`（`soxr.ResampleStream(src_rate, 48000, 1, dtype="float32", quality="HQ")`、src==48000 はバイパス）→ 48k float32。
  3. **int16 量子化**して 48k int16 バッファへ連結: `(np.clip(y,-1,1)*32767).astype("<i2").tobytes()` を `self._buf += ...`。
  4. `self._buf` を `OPUS_FRAME_SIZE*2`(=1920B) 単位に切り、各フレームを `encoder.encode(frame_bytes, OPUS_FRAME_SIZE)` → パケットをリストに。端数は `self._buf` に繰越。
- **`flush()`（drain→padding 分離・codex#5）**: ①resampler を `self._rs.resample_chunk(np.empty((0,),float32), last=True)` で **drain** し int16 化して `self._buf` へ。②`self._buf` に残りがあれば **`OPUS_FRAME_SIZE` まで無音(0)で pad** して 1 パケット encode（無ければ 0 パケット）。`_stream_tts` 正常終了時に呼ぶ。
- **デコーダ（codex#13）**: `decoder_factory` 既定 = `lambda: opuslib.Decoder(OPUS_RATE, 1)`。`decode(packet) = decoder.decode(packet, OPUS_FRAME_SIZE)` → **常に 960 サンプル(1920B)** の 48k mono int16 bytes。送受双方 `OPUS_FRAME_SIZE` 固定合意（1パケット=1フレーム）。
- **注入 seam（codex#13）**: `encoder_factory`/`decoder_factory`/`resampler_factory` を分けて渡せ、fake で実 opuslib/soxr 無しのフレーミング境界テストが書ける。**往復テストは実 opuslib＋実 soxr で別途1本**（依存の確証・§4）。
- **境界**: `common/` は client/server 双方が import。opuslib/soxr/numpy は forbidden 集合に**含めない**（torch/qwen/silero/PySide6/sounddevice のみ禁止）。opuslib/soxr は**関数内/factory内で遅延import**（pcm 専用環境や `--check` を邪魔しない）。numpy は base 依存で top-level 可。

### 3.2 プロトコル / ネゴシエーション
- **AudioFormatPayload は不変**（codec/sample_rate/channels のみ）。opus は **固定定数**（`opus_codec.OPUS_*`）で運用するため frame_ms 等の新フィールドは作らない（送受双方が `common/opus_codec.py` の定数・`opus_wire_format()` を共有・codex#8）。
- **クライアント caps**: `Hello.caps = {"codecs": [...]}`。`opus_codec.opus_available()` かつ `client.toml [server].codec=="opus"`（既定 "opus"）なら **`[OPUS_CODEC_NAME, "pcm_s16le"]`**（opus 優先）、否なら `["pcm_s16le"]`。opuslib/libopus 不在なら**自動で pcm のみ**を出す（client log に1回 warning・codex#9,#11）。
- **サーバ採用判定（`_handshake`・session 単位固定・codex#2）**: `chosen = OPUS_CODEC_NAME if (OPUS_CODEC_NAME in hello.caps.codecs) and (normalize_transport_codec(cfg.transport.codec)==OPUS_CODEC_NAME) and opus_codec.opus_available() else "pcm_s16le"`。`session.codec = chosen`（**接続中固定**）。`Ready.codec = chosen`。
  - **`Ready.limits["opus"]` は必ず出す（codex#8）**: `{"opus": {"available": opus_codec.opus_available(), "rate":48000, "frame_ms":20, "bitrate":cfg.transport.opus_bitrate}}`。クライアントは `AudioBegin.format` が `opus_wire_format()` と一致しなければ pcm 降格 or error（不一致検知）。
  - **fallback 可視化（codex#9）**: `cfg.transport.codec=="opus"` なのに opus を採用しなかった場合（caps 非対応 or opuslib 不在）、**server log に warning**（理由付き）。silent にしない。
- **per-stream（codex#2: session 固定で一貫）**: `AudioBegin.format.codec = session.codec`。**opus のとき `format = opus_wire_format()`（48000/1）**。pcm のとき従来通り profile の rate。`Ready.codec == 全 AudioBegin.format.codec` を**不変条件**に保つ（per-stream fallback はしない）。
  - **多ch TTS（codex#2）**: opus は mono 固定なので、profile が多chでも `_stream_tts` が **mono ダウンミックスしてから encode**（§3.3）。コーデックは stream ごとに切り替えない。
- **後方互換**: caps に opus を出さない C1/C2 クライアント → サーバは pcm 採用 → 既存挙動完全維持。`transport.codec="pcm"` → 常に pcm。
- **`normalize_transport_codec(s)`（codex#15）**: 設定値 `"pcm"`/`"opus"` を wire 名へ正規化する一元関数（`"pcm"→"pcm_s16le"`, `"opus"→OPUS_CODEC_NAME`、未知は `"pcm_s16le"` にフォールバック＋warning）。`server/config.py` か `common` に置く。

### 3.3 サーバ: `_stream_tts` への Opus 差し込み（`orchestrator.py`）
- `self.state.session?` ではなく `Session.codec`（`_handshake` で確定・接続中固定）を見て分岐:
  - **pcm**: 従来通り `AudioBegin(format=profile rate)` → `tts_client.stream` の PCM を `_send_bytes` → `AudioEnd`。
  - **opus**: `AudioBegin(format=opus_wire_format())`（48000/1） → `enc = OpusStreamEncoder(src_rate=profile.audio.sample_rate, bitrate=cfg.transport.opus_bitrate)` → `async for chunk in tts_client.stream(...):` で、**profile が多chなら chunk を mono ダウンミックス**（`_pcm_to_float32(chunk, codec, ch).mean(axis=1)` → `<i2` bytes。byte 端数は encoder の `_input_carry` が吸収するので chunk 単位 downmix で可、ただし端数は downmix 前に carry すること＝**encoder 入力は常に mono int16 bytes 契約**）→ `for pkt in enc.feed(mono_chunk): await _send_bytes(pkt)` → 末尾 `for pkt in enc.flush(): await _send_bytes(pkt)` → `AudioEnd`。
  - 実装上は **多ch downmix を `_stream_tts` 側で行い、`OpusStreamEncoder` には常に mono int16 を渡す**（§3.1 の mono 固定契約）。MVP の GPT-SoVITS profile は 32k mono なので通常は downmix 不要。
- **cancel/error 経路は不変**（`CancelledError`→`stopped`、例外→`error`）。エンコーダはタスクローカル（`_stream_tts` 内生成）なので状態リークなし。`Session.codec` は接続単位固定で per-stream に切り替えない（codex#2: `Ready.codec==全AudioBegin.codec`）。
- GPU/STT 経路には影響しない（opus は TTS 出力のみ。auto_tts 連鎖の TTS も同じ `_stream_tts` 経由なので自動的に opus 化）。

### 3.4 クライアント: Opus デコードを binary 経路へ（`ws_client.py`）
- **caps**: `connect` の `Hello` を `caps={"codecs": self._client_codecs()}` に。`_client_codecs()` は `opus_codec.opus_available()`＋`codec_pref=="opus"` で `[OPUS_CODEC_NAME,"pcm_s16le"]`、否なら `["pcm_s16le"]`（opuslib 不在は自動 pcm のみ＋1回 warning）。
- **AudioBegin 受理時（codex#1: wire と playback PCM を分離）**: `self._playback_codec = msg.format.codec`。
  - **`opus` のとき**: ①`msg.format` が `opus_wire_format()`（codec=opus/48000/1）と**一致するか検証**、不一致なら terminal error（codex#8）。②`self._opus_decoder = OpusStreamDecoder()`。③**sink には PCM フォーマットを渡す**: `sink.begin(AudioFormatPayload(codec="pcm_s16le", sample_rate=48000, channels=1), device)`（**`msg.format`(=opus) を渡さない**＝`StreamingPlayer` が `bytes_per_sample("opus")` で落ちるのを防ぐ・codex#1）。`StreamingPlayer` は 48k PCM をデバイスrateへ soxr。
  - **`pcm` のとき**: 従来通り `sink.begin(msg.format or _ready.tts, device)`、`_opus_decoder=None`。
- **binary 受信（`_on_binary`）**: `_playback_codec=="opus"` のとき **`pcm = self._opus_decoder.decode(raw)` してから `sink.feed(pcm)`**。`pcm` のときは従来通り `sink.feed(raw)`。overload/TTFA ログは feed 後（PCM 換算）で従来通り。
  - **decode 例外は terminal 仕様化（codex#7）**: `try: pcm=decode(raw) except Exception: self._sink.abort(); self._log("opus decode 失敗"); ` → `_cancelling=True`＋`await _send(Cancel(_active_id))`（loop 経由）＋`_finish_stream()`＋`_state("error", ...)`。ログだけにしない（受信ループが side-band binary を食い続けない）。
- **`_finish_stream`**: 既存に加え `_playback_codec=None`・`_opus_decoder=None` もリセット。
- **token は `Authorization: Bearer` 既定（codex#4）**: `connect` で token があれば `websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"})`（websockets 16.0 は `additional_headers`）。**query への token 付与は legacy fallback**（`client.toml [server].token_in_query=false` 既定。true で従来の `?token=` も付ける）。サーバは両対応（§3.5）。
- **TLS（wss）**: `address` が `wss://` の場合 **ssl context を構築して `websockets.connect(url, ssl=ctx, ...)`**。`ctx`: `tls_cafile` 指定→`ssl.create_default_context(cafile=...)`、`tls_insecure=True`→`ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE`、無指定→既定（公的CA検証）。`ws://` は ssl 無し。`ssl` は標準ライブラリ（新規依存なし）。

### 3.5 セキュリティ詳細
- **token 検証（`ws_server.py`・Bearer 既定・定数時間比較・codex#4,#12）**: 取得順は **①`Authorization: Bearer <token>` ヘッダ（推奨/既定）→ ②`?token=` query（legacy fallback）**。比較は **`import hmac; hmac.compare_digest(presented, token)`**（定数時間）。不一致は `close(1008)`。空 token（`bind.token==""`）は検証スキップ（loopback ローカル運用）。
- **TLS 無し外部 bind の警告（codex#4）**: `validate_bind` を通った上で、`bind.host` が非 loopback かつ **TLS 未設定**（cert/key 空）なら **起動時に警告ログ**（「token は平文で流れる。wss/TLS 推奨」）。token 必須ルール自体は維持。
- **wss/TLS（サーバ）**: `BindConfig` に `tls_cert: str=""`/`tls_key: str=""` 追加。`app.py run_server`: `validate_tls(cfg.bind)` 後、両方非空なら `uvicorn.run(app, host, port, ssl_certfile=cfg.bind.tls_cert, ssl_keyfile=cfg.bind.tls_key)`、両空なら従来（平文）。
- **`validate_tls(bind)`（新規・codex#10）**: `tls_cert`/`tls_key` は **両方指定 or 両方空**（片方のみは `ValueError`）。指定時は**ファイル存在確認**（無ければ `ValueError`）。`validate_bind` とは別関数。起動前（`run_server`）と設定ロードで呼ぶ。`tls_insecure` は **client 側のみの概念**（server config には置かない）。
- **自己署名手順**（SETUP/本書に注記）: `openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=<host>"`。client は `tls_cafile=cert.pem`（推奨）or `tls_insecure=true`（検証無効・LAN検証用）。**リバースプロキシ(nginx/caddy)終端**も可（その場合 server は平文 loopback、proxy が wss 終端＝SETUP 注記）。
- **脅威モデル注記**: token は WS 接続時のみ検証（接続後はセッション信頼）。Bearer ヘッダ既定でアクセス/プロキシログへの token 漏洩を低減。TLS で盗聴/改ざんを防ぐ。MVP は単一 token（マルチユーザ/失効は将来）。

### 3.6 設定拡張
- `server.toml`:
  - `[bind]` に `tls_cert = ""` / `tls_key = ""`（両指定で TLS 有効・片方のみは `validate_tls` で起動拒否）。
  - `[transport]` に `codec = "opus"`（"pcm"|"opus"。既定 **"opus"**＝非対応クライアント/opuslib不在には自動 pcm フォールバック＋warning）と `opus_bitrate = 24000`（bps）。
- `client.toml`:
  - `[server]` に `codec = "opus"`（"opus"|"pcm" 優先指定）・`tls_insecure = false`・`tls_cafile = ""`・`token_in_query = false`（既定は Bearer ヘッダのみ。true で legacy query も付与）。
- `config.py`（両側）: `TransportConfig` に `opus_bitrate:int=24000`、`BindConfig` に `tls_cert`/`tls_key`、client `ServerConfig` に `codec`/`tls_insecure`/`tls_cafile`/`token_in_query` 追加。`_only`/dict 防御は既存流儀。`run_check`/`run_server` の表示に codec/TLS 有無を1行追加（codex#9 の可視化を兼ねる）。
- **`normalize_transport_codec(s)`（codex#15）**: `"pcm"→"pcm_s16le"`, `"opus"→OPUS_CODEC_NAME`, 未知→`"pcm_s16le"`＋warning。設定値→wire名 変換を一箇所に。

### 3.7 フォーマット/フレーミング（要点再掲）
- **opus 固定**: 内部 48000Hz / 20ms / mono / `frame_size=960`。1 WS バイナリ = 1 Opus パケット。`opuslib.APPLICATION_AUDIO`（TTS 音声の自然さ重視・codex#14）。
- サーバ: TTS 32k →（多chなら mono downmix →）float32 soxr 48k → clip→<i2 → 960サンプル枠 → encode。クライアント: decode（常に960サンプル）→ 48k PCM → `StreamingPlayer`(48k→device)。
- bitrate 既定 24000bps（音声品質十分）。**帯域目安（codex#16）**: 1秒 32k mono PCM=64000B に対し 24kbps Opus は payload 合計 概ね **3000〜8000B/s**（~1/8〜1/20）。

## 4. 受け入れ基準
- [ ] **opus_codec 往復ユニット**（実 opuslib・soxr）: 32000Hz mono 1秒の合成 PCM を `OpusStreamEncoder(src_rate=32000)` で encode→パケット列、`OpusStreamDecoder` で decode→48k PCM。**各 Opus packet decode が常に 960 サンプル**、**総 decoded サンプル ≈ 48000（+flush padding ≤960、つまり 48000〜48960）**、パケット非空、`flush` の packet 数 0|1、を**別々に**検証（codex#3,#5）。16000Hz/48000Hz でも往復可。**入力を半端バイト長で feed しても境界 carry で壊れない**（codex#6）。
- [ ] **フレーミング境界ユニット**（fake encoder/decoder 注入）: feed の完成フレーム数・残差繰越・flush の drain→padding を実 opuslib 無しで検証（codex#5,#13）。
- [ ] **ネゴシエーション・ユニット**: caps=`["opus","pcm_s16le"]`＋`transport.codec="opus"`＋opus_available → `Ready.codec=="opus"`、`Ready.limits["opus"].available==true`。caps=`["pcm_s16le"]` → `"pcm_s16le"`（**C1/C2 後方互換**）。`transport.codec="pcm"` → 常に pcm。opus 要求だが非対応時は **warning** が出る（codex#9）。`fastapi.testclient` の WS で `Ready` を確認。
- [ ] **クライアント opus デコード経路**（fake WS サーバ・`asyncio.run`）: サーバが `AudioBegin(format=opus_wire_format())`＋**実 opus パケット**×N＋`AudioEnd` を返すと、`WSClient` が `OpusStreamDecoder` でデコードし **fake sink が PCM を feed**（バイト数 > 0・begin/feed/end）。**fake sink の begin codec が `pcm_s16le`（opus でない）**を検証（codex#1）。**decode 例外で abort+idle+error**（codex#7）。`codec="pcm"` 経路は従来通り（リグレッション無し）。
- [ ] **token（TestClient.websocket_connect・codex#12）**: 正token を **Authorization ヘッダ**で=accept、正token を **query**で=accept（legacy）、誤token=close(1008)、空token設定=検証スキップで accept。`hmac.compare_digest` 使用。
- [ ] **TLS 設定の配線**（ユニット/スモーク）: `validate_tls` が cert/key 片方のみ=`ValueError`・両空=OK・両指定&ファイル不在=`ValueError`（codex#10）。両指定で `run_server` が uvicorn に `ssl_certfile/ssl_keyfile` を渡す（uvicorn を mock して引数検証）。client が `wss://` で ssl context を構築（`tls_insecure`/`tls_cafile` 分岐）。非 loopback bind かつ TLS 無しで**警告ログ**（codex#4）。
- [ ] **後方互換**: 既存 `tests/test_ws_*`/`test_ws_client`/C2 系が全 green（caps に opus を足しても pcm 経路不変、AudioFormatPayload 不変）。
- [ ] **境界/コンパイル**: AST 境界 green（opuslib/soxr はトップレベル import 禁止＝遅延/factory 注入、`common/` は torch/qwen/silero/PySide6/sounddevice を import しない）。py_compile・全 pytest green。
- [ ] **opuslib 起動プローブ（codex#11）**: client `--check`（と server 起動時）に `opus_codec.opus_available()` を表示。不在環境でも落とさず pcm 降格。
- [ ] **統合（手動・私がLinuxで）**: `transport.codec="opus"` でサーバ起動 → クライアント（opus caps）でテキスト/マイク → **opus で TTS 音声が再生**（先頭欠け無し）・**送出バイト量が PCM 比で 1/8 以下**をログで確認（codex#16）。`tls_cert/key`＋`wss://`＋`tls_cafile` で TLS 接続成功。token を Bearer で認証、不一致で接続拒否。
- [ ] **Windows実機（あなた）**: opuslib の libopus DLL 配置（§7）、`wss://`＋opus で再生、token(Bearer) 認証、自己署名CA。

## 5. 変更/新規ファイル
- `common/opus_codec.py`（**新規**: `OpusStreamEncoder`/`OpusStreamDecoder`・`OPUS_*` 定数・`opus_wire_format()`・`opus_available()`）
- `server/orchestrator.py`（`_handshake` で codec 採用・`Session.codec`・`Ready.limits["opus"]`・fallback warning・`_stream_tts` に opus 分岐＋mono downmix）
- `server/ws_server.py`（token を **Bearer 既定＋query fallback**・`hmac.compare_digest`・非loopback TLS無し warning）
- `server/config.py`（`BindConfig`+tls_cert/tls_key、`TransportConfig`+opus_bitrate、`validate_tls()`、`normalize_transport_codec()`）
- `server/app.py`（`validate_tls` 後 TLS 時 uvicorn に ssl 引数・opus_available 表示）
- `server.toml`（`[bind]` tls_*・`[transport]` codec="opus"/opus_bitrate）
- `client/ws_client.py`（caps に opus・AudioBegin で OpusStreamDecoder＋**sink へ PCM fmt**・binary デコード＋例外 terminal・**Bearer ヘッダ**＋wss ssl 注入・`_playback_codec`/`_opus_decoder` リセット）
- `client/config.py`（`ServerConfig`+codec/tls_insecure/tls_cafile/token_in_query）
- `client.toml`（`[server]` codec/tls_insecure/tls_cafile/token_in_query）
- `client/app.py`/`run_check`（`opus_available()` 表示・起動プローブ）
- `pyproject.toml`（client/server extra に `opuslib>=3.0.1`）
- `docs/SETUP.md`（自己署名 cert 手順・Windows libopus DLL 配置・リバースプロキシ注記）
- `tests/test_opus_codec.py`（**新規**）・`tests/test_ws_negotiation.py`（**新規**：ネゴシエーション＋token＋TLS配線）・`tests/test_ws_client.py`（opus デコード経路＋sink begin codec＝pcm＋decode例外）・`tests/test_import_boundaries.py`（opuslib 遅延確認）

## 6. テスト/検証
- **ユニット（GPT-SoVITS不要）**:
  - `test_opus_codec.py`: 実 opuslib で 32k/16k/48k 往復、パケット非空、サンプル数概算、flush 端数、注入 fake でのフレーミング境界（残差繰越）。
  - ネゴシエーション: `_handshake` 経由 or 単体関数で chosen codec 判定（opus/pcm/フォールバック/後方互換）。`fastapi.testclient` の WS で `Ready.codec` を確認してもよい。
  - `test_ws_client.py`(追加): fake WS サーバが実 opus パケットを返し、client が decode→fake sink feed（PCM bytes>0）。pcm 経路リグレッション維持。
  - token: `ws_server` の比較が `compare_digest`、正/誤/空。
  - TLS 配線: `run_server` の uvicorn 呼び出し引数（mock）／client ssl context 構築分岐。
- **統合（手動）**: §4。サーバ＋実 GPT-SoVITS。opus 再生・帯域減・wss/TLS・token。Linux は私、Windows はあなた。
- AST境界・py_compile・全 pytest green 維持。

## 7. リスク・留意点
- **Opus は 32kHz 非対応**（対応=8/12/16/24/48k・実測確認済み）。TTS 32k は **必ず 48k へリサンプルしてから encode**。クライアントは 48k で decode し `StreamingPlayer` がデバイスrateへ。ここを誤ると音程/速度が壊れる。
- **フレーミング厳守**: encode/decode は `frame_size=960`（48k×20ms）固定。送信1パケット=1フレーム。decoder は同じ frame_size で復号。端数は encoder 内で繰越し、`flush` で無音パディングして送出（末尾切れ防止）。
- **opuslib の素性**: ctypes で `libopus.so.0` を呼ぶ（当環境に存在）。`decoder.py` に無害な `SyntaxWarning`（"is not" int）あり＝動作影響なし。Windows では libopus DLL の同梱/配置が必要（opuslib wheel or 同梱、SETUP に注記。Windows 検証はあなた）。
- **後方互換が最重要**: caps に opus を足しても、サーバが pcm を選べば C1/C2 と完全同一。`AudioFormatPayload` に新フィールドを足さない（opus は定数運用）ことで既存テスト・既存クライアントを壊さない。**既存 pytest 全 green を必須条件**にする。
- **クライアント opus デコードはイベントループ上**（`_on_binary` は recv_loop 内・同期）。opuslib decode は軽量（~960サンプル）だが、重い場合は将来 sink ワーカーへ移譲（MVP はループ上で可・実測で確認）。
- **TLS**: uvicorn ネイティブ TLS は単一証明書。自己署名は client 側 `tls_cafile`（推奨）/`tls_insecure`（LAN検証）で受ける。`ssl` は標準ライブラリ。リバースプロキシ(nginx/caddy)終端も可（SETUP 注記）。`validate_bind` の token 必須ルールは TLS と独立に維持。
- **アップリンク非対象**: マイク→STT は PCM 16k 維持（§1）。Opus 化は STT 精度影響と小帯域のため将来。混同しない。
- **新規依存 `opuslib`**: core dependencies に追加（common/opus_codec は client/server 双方が import する共有モジュールのため。extra ではなく `[project.dependencies]` ＝`uv run` で常時導入）。`uv run --with opuslib` で往復実測済み。`silero`/`pyogg`/`av(PyAV)` 等は**追加しない**（PyAV は既存だが streaming raw packet が煩雑なため opuslib を採用）。
- **GPU/STT 不変**: opus は TTS 出力のみ。STT 直列化（single-worker executor）・auto_tts 連鎖・cancel 三者照合は X1 で変えない。
- **opus 時 sink は PCM フォーマット（codex#1）**: `AudioBegin.format.codec="opus"` を**そのまま sink/player に渡さない**（`StreamingPlayer` が `bytes_per_sample("opus")` で落ちる）。ws_client が **wire(opus) と playback(pcm_s16le/48000)** を分離し、decode 後の PCM を sink へ。
- **session 単位コーデック固定（codex#2）**: `Ready.codec == 全 AudioBegin.format.codec`。per-stream で codec を切り替えない。opus は mono 固定＝多ch TTS は `_stream_tts` で mono downmix。
- **float32 リサンプル経路（codex#3）**: int16 直 soxr ではなく `_pcm_to_float32 → ResampleStream(float32) → clip → <i2`（既存 player と同方式・実測確認済み）。誤ると音程/速度/サンプル数が壊れる。frame_size=960 固定で送受合意。
- **入力バイト境界 carry（codex#6）**: TTS HTTP chunk は sample 境界に揃わない。encoder は完全サンプル境界で切ってから resampler へ（`_input_carry`）。
- **token は Bearer 既定（codex#4）**: query token はログ漏洩しやすい。`Authorization: Bearer` を既定経路にし query は legacy fallback。比較は `hmac.compare_digest`。非loopback TLS無しは警告。
- **opus 不在の可視化（codex#9,#11）**: opuslib/libopus 不在でも落とさず pcm 降格するが、**warning を必ず出す**（運用者が「opus のつもりで pcm」を検知）。`opus_available()` を client/server の起動表示に。
- **Windows の libopus（codex#11）**: opuslib は libopus を ctypes で探す。Windows は `opus.dll`/`libopus-0.dll` の同梱・PATH 配置が必要（SETUP に手順）。client は起動プローブで早期検知し pcm 降格。**Windows 実機検証はあなた**。
- **新規依存 `opuslib`**: core dependencies に追加（common/opus_codec は client/server 双方が import する共有モジュールのため。extra ではなく `[project.dependencies]` ＝`uv run` で常時導入）。`silero`/`pyogg`/`av(PyAV)` 等は追加しない（PyAV は既存だが streaming raw packet が煩雑なため opuslib を採用）。
