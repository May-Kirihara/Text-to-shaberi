# X3 実装計画書 v2: 持続的プレイバックエンジン（出力ストリーム常時開放）

> 由来: 先頭欠け修正(`d381e88`)の `prime_ms`(既定500)無音プリロールは**毎発話に固定レイテンシ**を足す。X3 は**出力 OutputStream を発話間で開きっぱなしにし、ウォームアップを1回に償却**。アイドル時は無音 keepalive で device を warm 維持し、prime レイテンシを**ゼロ化**（頭欠けもゼロ）。
> フロー: 本書 → codexレビュー(`docs/REVIEW-codex-X3.md`) → **v2改訂** → GLM委託 → 私が**実機(null sink録音)で検証**/統合 → `docs/CODE-REVIEW-X3.md`。**この仕様の通りに実装すること。**
> **v2: codexレビュー反映** = ①connect 後 **eager prewarm＋warm barrier**で初回頭欠けゼロ保証・②**協調 abort**(実音声 write を ≤20ms 分割し abort flag を見る)＋stream は閉じない・③**seq 方式の単一所有 worker**(毎発話 `_BEGIN(seq,fmt,device)`・全 item に seq・worker のみが `_cur/_resampler/_carry/_stream` 操作・古い seq 破棄・drain しない)・④WSClient **`_engine`(持続)/`_playback`(発話)分離**・⑤**worker 例外復旧**(lock下で状態リセット)・⑥keepalive write ブロック計測・⑦`flush_resampler` helper(`resample_chunk(empty,last=True)`)・⑧`_BEGIN` で carry/resampler 必ず reset・⑨**`engine_factory` 新設**(`sink_factory` は legacy 残置)・⑩device 変更 **prewarm**・⑪検証は **cancel→無音時間**計測・⑫**mono のみ**明記・⑬sentinel 型明記・⑭call_soon_threadsafe assert・⑮prime 三重状態の優先順位明記。
> 前提: headclip 修正完了（`d381e88`）。**新規依存ゼロ**。
> **検証必須**: 頭欠けは **RecordingSink(fake) では検出不可**だった反省から、X3 は **実 OutputStream を実 sink(null sink monitor 録音)で再生**し「初回/2回目以降/長時間アイドル後 の頭欠けゼロ」「cancel→無音までの時間」を必ず実測する。

## 1. 目的・スコープ
- スコープ内:
  - **持続的 `PlaybackEngine`**（`client/audio/engine.py` 新規）: **1本の OutputStream ＋ 1本の常駐 worker**を複数発話で再利用。アイドル時は無音 keepalive で warm 維持。**connect 後に eager prewarm**（warm barrier）して初回頭欠けゼロ。
  - 発話 API は `PlaybackSink` 同形（`begin/feed/end/abort/close/join`）だが **WSClient はエンジンを1個だけ生成し全発話で再利用**（`engine_factory` 注入）。
  - **seq 方式の単一所有 worker**で状態 race を排除。**協調 abort** で stream を閉じず warm 維持。
  - **device 変更**は明示 `set_device(device)`/prewarm で旧 stream を閉じ新 device を idle prewarm。**source rate 変更**は resampler 再生成のみ（stream 維持）。
  - prime は engine 経路では**不要**（keepalive＋prewarm）。設定の優先順位を明記（§3.6）。
- スコープ外:
  - 本格 AEC・デバイスホットスワップ自動検出。**多ch 出力**（MVP は **mono のみ**・多ch は拒否ログ＋mono ダウンミックス済み前提）。
  - アイドル長時間後の省電力 auto-close（将来オプション・§7）。MVP は keepalive 常時。
  - サーバ/STT/コーデック不変。

## 2. 既存資産
- `client/audio/sink.py` `PlaybackSink`: 毎発話 worker＋`StreamingPlayer` 生成→`end`/`abort` で close＝**毎発話 OutputStream 開閉**。非ブロッキング feed/end/abort・有界 queue(256)・worker ログ `call_soon_threadsafe`。**残置（legacy）**。
- `client/audio/player.py` `StreamingPlayer`: src→device soxr リサンプル・遅延スタート・preroll・prime・`request_abort`/`close`・factory 注入。**リサンプルロジックを engine が流用**（`_pcm_to_float32`/`split_full_samples`/`soxr.ResampleStream`/`_device_samplerate`/`preferred_default_output`）。`close()` の `resample_chunk(empty, last=True)` を **`flush_resampler()` helper に切り出して共有**。**残置（legacy）**。
- `client/ws_client.py`: `_on_message` AudioBegin で `_sink=sink_factory(...); _sink.begin(fmt, device)`。binary→`_sink.feed`・AudioEnd→`_sink.end()`+`_finish_stream`・cancel/overload→`_sink.abort()`・close→`_sink.abort()`+`to_thread(join)`。opus は `_on_binary` でデコードし pcm_s16le/48000 を feed。`_finish_stream` で `_sink/_active_id/_playback_id/_playback_codec/_opus_decoder` をリセット。X2a の接続ライフサイクル(`_conn_lock`/reconnect/close)。
- `client/app.py`: WSClient 配線・`prime_ms=cfg.output.prime_ms`・`device_provider=window.selected_output_device`・設定変更で reconnect/mic 再生成。
- `client/audio/devices.py`: `preferred_default_output()`・`resolve_device(...)`。

## 3. 設計

### 3.1 `client/audio/engine.py`: `PlaybackEngine`（新規・持続・seq 単一所有 worker）
```python
@dataclass(slots=True)
class _Begin: seq:int; fmt:AudioFormatPayload; device:object
@dataclass(slots=True)
class _Audio: seq:int; data:bytes
@dataclass(slots=True)
class _End:   seq:int
_ABORT = object()     # seq 不要(現発話を打ち切り keepalive へ)
_CLOSE = object()     # worker 終了

class PlaybackEngine:
    def __init__(self, *, on_log=None, loop=None,
                 keepalive_ms=20, warm_min_ms=300, max_write_ms=20,
                 queue_max=512, stream_factory=None, resampler_factory=None) -> None: ...
    # 起動/終了
    def prewarm(self, device) -> None: ...   # connect 後: stream open+worker起動+keepalive。warm barrier まで待てる
    def set_device(self, device) -> None: ...# device 変更: 旧stream閉じ新deviceでidle prewarm（_BEGIN前に呼ぶ/begin内で検知でも可）
    def close(self) -> None: ...             # _CLOSE→worker停止→stream stop/close
    def join(self, timeout=2.0) -> None: ...
    # 発話 API（PlaybackSink 互換・全て非ブロッキング/同期）
    def begin(self, fmt, device) -> None: ...# seq++; 必要なら set_device; _q.put(_Begin(seq,fmt,device))
    def feed(self, chunk) -> bool: ...        # _q.put_nowait(_Audio(seq,chunk)); 満杯 False
    def end(self) -> None: ...                # _q.put_nowait(_End(seq))
    def abort(self) -> None: ...              # seq++（以降の旧seq item を無効化）; _q.put_nowait(_ABORT)
```
- **状態（worker だけが触る）**: `_stream`・`_target_sr`・`_device_cur`・`_resampler`・`_carry`・`_cur_fmt`・`_play_seq`(再生中の seq)。**main 側が触るのは `_q` と `_seq`(begin/abort で ++、Lock or itertools.count)・起動/終了フラグのみ**。
- **seq 規律（critical#3）**: `begin` で `self._seq` を ++ し `_Begin(seq,...)` を enqueue。`feed`/`end` はその時点の `self._seq` を載せる。`abort` は `self._seq` を ++（以降到着する旧 seq の `_Audio/_End` は worker が**古い seq として破棄**）。**queue を drain しない**（drain race を避ける）。worker は `_Begin` 受信で `_play_seq=seq`＋carry/resampler を reset（#8）、`_Audio/_End` は `item.seq==_play_seq` のときだけ処理、違えば破棄。
- **`prewarm(device)`（初回 warm 保証・critical#1）**: stream を open（device 解決→`_target_sr`→OutputStream 生成→`start()`）し worker 起動。worker は即 keepalive 無音を書き始める。`prewarm` は **`warm_min_ms` 分の keepalive write が完了するまでブロック待ち**できる（threading.Event で worker→main 通知）。WSClient は connect 成功直後に `prewarm` を呼び、最初の `begin` までに warm 済みを保証。
- **worker `_run`（常駐・単一所有）**:
  ```
  silence = zeros(keepalive_frames @ _target_sr)
  warmed_frames = 0
  try:
    while True:
      try: item = self._q.get(timeout=keepalive_ms/1000)
      except Empty:
          t0=...; self._stream.write(silence); blk=elapsed
          if blk > keepalive_ms*3: self._log(f"keepalive write blocked {blk}ms")   # should#1
          warmed_frames += keepalive_frames
          if warmed_frames >= warm_min_frames and not self._warm.is_set(): self._warm.set()
          continue
      if item is _CLOSE: break
      if item is _ABORT:
          self._carry=b""; self._reset_resampler(); continue   # stream維持・keepaliveへ（協調abortの仕上げ）
      if isinstance(item, _Begin):
          if device/target 変更: self._reopen(item.device)
          self._cur_fmt=item.fmt; self._make_resampler(item.fmt); self._carry=b""; self._play_seq=item.seq
          continue
      if isinstance(item, _End):
          if item.seq==self._play_seq:
              tail = flush_resampler(self._resampler)   # resample_chunk(empty,last=True)
              if tail is not None and len(tail): self._stream.write(tail)
          continue   # stream維持
      if isinstance(item, _Audio):
          if item.seq != self._play_seq: continue        # 古い発話 → 破棄
          frames = self._decode_resample(item.data)       # carry→_pcm_to_float32→resampler.resample_chunk
          self._write_chunked(frames)                      # ≤max_write_ms で分割 write、各 write 前に seq 変化(abort)を見る
  except Exception as exc:
      self._log(f"playback worker 例外: {exc}")
  finally:
      with self._lock:
          try: self._stream and self._stream.stop(); self._stream.close()
          except: pass
          self._stream=None; self._started=False; self._thread=None   # 復旧可能状態へ（critical#5）
  ```
- **`_write_chunked(frames)`（協調 abort・critical#2）**: frames を `max_write_ms`(=20ms@target_sr) 単位に分割し、各サブ write の**前に `_play_seq` が変わっていないか（=abort/次begin が来ていないか）を確認**。変わっていたら残りを捨てる。これで「write 実行中に abort しても ≤ 1 サブ write(≤20ms)＋device buffer(latency=low の数ms〜数十ms) で停止」＝interrupt SLO 内。**`stream.abort()` は呼ばない**（呼ぶと再 warm 必要＝頭欠け再発）。
- **`_reopen(device)`（device 変更・should#5）**: lock 下で旧 stream stop/close→新 device 解決→`_target_sr` 更新→OutputStream 生成→`start()`→keepalive 再開。reopen 後 `warm_min_ms` の keepalive を経て warm（変更後初回も barrier 可・または「変更後初回は短い再 warm 許容」と明記）。
- **`feed`**: `try: _q.put_nowait(_Audio(self._seq, chunk)); return True except Full: return False`（非ブロッキング・満杯は呼び出し側 abort）。
- **mono 固定（should#7/#12）**: OutputStream は mono(channels=1)。`fmt.channels>1` は **拒否ログ**（呼び出し側 ws_client は既に mono の PCM を渡す契約＝opus は mono デコード、pcm TTS も mono profile）。channels 変更は reopen 条件に含めるが MVP は mono 前提。
- **境界/注入**: sounddevice/soxr 遅延import/`stream_factory`/`resampler_factory` 注入。`numpy` top-level 可。worker ログは `loop.call_soon_threadsafe`（loop あれば）。

### 3.2 `client/ws_client.py`: `_engine`(持続)/`_playback`(発話)分離（critical#4）
- `__init__`: 新規 `engine_factory: Callable = PlaybackEngine`（**`sink_factory` は legacy 残置・engine 経路では未使用**）。`self._engine = None`。
- **connect 成功時**: `self._engine = self._engine_factory(loop=self._loop, on_log=self._on_log)` し **`self._engine.prewarm(self._resolve_device())`**（初回 warm 保証）。既に engine があれば再利用（reconnect では閉じない）。
- **AudioBegin 受理**: engine を作り直さず `self._engine.begin(fmt_or_pcm_fmt, device)`。opus は pcm_s16le/48000、pcm は `msg.format or _ready.tts`。`self._playback_id=msg.id`・`_playback_codec`・`_opus_decoder` を設定。
- **`_on_binary`**: ガードを **`_active_id and _playback_id and not _cancelling`** に（`_engine is not None` では判定しない）。opus は decode→`_engine.feed(pcm)`、pcm は `_engine.feed(raw)`。満杯 False→`_engine.abort()`＋cancel（現行同様）。
- **AudioEnd**: `_engine.end()`＋`_finish_stream`。**cancel/overload**: `_engine.abort()`。
- **`_finish_stream`**: `_playback_id/_playback_codec/_opus_decoder/_cancelling/_first_chunk_logged/_active_id/_stt_auto_tts` をリセット。**`_engine` は残す**（持続）。
- **device 変更（出力切替/設定）**: `begin(fmt, device)` の device が変われば engine が `_reopen`。即時切替したい場合は app から `ws.set_output_device()`→`engine.set_device()` を呼ぶ経路も可（should#5）。MVP は次 begin で reopen＋短い再 warm 許容。
- **`close()`**: `_user_closing=True`（X2a）→ `_cancel_reconnect_task`→ `_teardown_ws`。**engine は `close()`**（`if self._engine: self._engine.abort(); await asyncio.to_thread(self._engine.close); self._engine=None`）。
- **`reconnect()`（X2a・設定保存）**: `_teardown_ws`＋`connect`。**engine は閉じない**（warm 維持）。connect の prewarm は engine 既存なら skip（再利用）。device 変更があれば次 begin/set_device で反映。
- prime は engine 経路で不要（preroll/prime 廃止）。

### 3.3 `flush_resampler` helper（should#2）
- `client/audio/player.py`（or engine.py）に `def flush_resampler(rs, channels) -> np.ndarray|None: return rs.resample_chunk(empty(channels), last=True) if rs else None` を切り出し、`StreamingPlayer.close()` と engine `_End` で**同一 API**を使う。

### 3.4 後方互換・既存テスト（should#4）
- **`engine_factory` を新設**し、`sink_factory` は legacy（PlaybackSink）として残す。live 経路は engine。
- 既存 `tests/test_ws_client.py` は `sink_factory=FakeSinkFactory` を注入し begin/feed/end/abort を記録。X3 で live 経路が engine に変わるため、**これらのテストは `engine_factory=FakeEngineFactory` に切替**（Fake は同 begin/feed/end/abort/prewarm/close/join インターフェース）。**エンジン1個再利用**なので Fake は records 累積に耐える実装にし、各テストの assert は `any(...)`/区間で判定（厳密 count は調整）。
- `tests/test_playback_sink.py`/`test_player_lazy_start.py` は **不変（legacy 残置）**。新規 `tests/test_playback_engine.py`。

### 3.5 検証（実機・客観計測）（critical#1,#2 / should#6）
- **null sink 録音**（`pactl load-module module-null-sink`→`set-default-sink`→device=None 再生→`parecord --device=<sink>.monitor`）で:
  1. **初回発話**: prewarm 済みで頭欠けゼロ（先頭立上り≈発話の自然な無音のみ）。
  2. **2〜3回目**: 同一 stream 再利用で頭欠けゼロ・再 warm 無し。
  3. **長時間アイドル(>5s)後**: keepalive で SUSPEND せず頭欠けゼロ（`pactl list sinks` で sink が idle 後も RUNNING 相当か確認・should#1）。
  4. **prime_ms=0** で上記成立（prime 無音レイテンシ無し）。`prime_ms=500`(legacy) との比較も残す。
  5. **abort/停止の SLO**: **cancel 送信時刻 → 録音 RMS が閾値未満になるまでの時間**を測り ≤150ms（should#6）。
  - tone-from-0（高SNR）＋実 GPT-SoVITS 発話の両方。録音窓は発話長＋余裕を十分とる（窓不足で末尾切れ＝有音長過少にしない）。

### 3.6 prime の三重状態の整理（nit#3）
- **engine 経路（X3 既定）**: prewarm＋keepalive で warm。**prime 不要 → `[output].prime_ms` 既定を 0 に変更**（コメントで「engine が warm 維持するため通常 0。legacy/StreamingPlayer 単体経路用」）。
- **StreamingPlayer 単体（legacy）**: `prime_ms` 引数の既定は据え置き（後方互換）。
- `config.OutputConfig.prime_ms` 既定を **0** に（engine が主経路のため）。client.toml も 0＋コメント。

## 4. 受け入れ基準
- [ ] **PlaybackEngine ユニット**（fake stream/resampler 注入・実 sounddevice 不要）:
  - `prewarm`→worker 起動・keepalive 無音 write 継続・`warm_min_ms` で warm Event セット。
  - `begin(seq)`→`feed×N(seq)`→`end(seq)` で resampled frames を write、**`end` で stream を close しない**。2回目 begin で**同一 stream 再利用**（reopen 無し）。
  - **seq 規律**: `abort`(seq++) 後に届く旧 seq の `_Audio/_End` を worker が**破棄**（drain せず）。back-to-back begin で前発話の残 audio が混ざらない。
  - **協調 abort**: write 分割（≤max_write_ms）中に abort(seq変更)で残りを捨てる。`stream.abort()` は呼ばれない。
  - **device 変更** `set_device`/begin(device変) で `_reopen`（fake stream 新規生成）。**source rate 変更**で resampler 再生成（stream 維持）。
  - **worker 例外復旧**: fake stream.write が一度例外→worker 終了→状態が lock 下で復旧（`_thread/_stream=None`）→次 `prewarm/begin` で再起動。
  - `feed` 満杯 False。worker ログ・warm 通知が **`loop.call_soon_threadsafe` 経由**（fake loop で assert・nit#2）。`fmt.channels>1` で拒否ログ。
- [ ] **後方互換**: 既存 `tests/test_ws_client.py`（speak/utter/auto_tts/cancel/error/reconnect）を `engine_factory` 注入へ移行し全 green。C1/C2/S2/X1/X2 系全 green。`test_playback_sink`/`test_player_lazy_start` 不変 green。
- [ ] **境界/コンパイル**: AST 境界（sounddevice/soxr 非トップレベル）・py_compile・全 pytest green。
- [ ] **実機検証（必須・私が Linux で）**: §3.5 の 1〜5。初回/2回目/idle後 頭欠けゼロ・prime_ms=0・cancel→無音 ≤150ms。
- [ ] **Windows実機（あなた）**: 連続発話頭欠け無し・prime無し低遅延・出力切替。

## 5. 変更/新規ファイル
- `client/audio/engine.py`（**新規**: `PlaybackEngine`＋sentinel dataclass＋seq worker＋prewarm/warm barrier＋協調abort）
- `client/audio/player.py`（`flush_resampler` helper 切り出し・`StreamingPlayer` は legacy 残置）
- `client/ws_client.py`（`engine_factory`・connect で engine 生成＋prewarm・AudioBegin で begin 再利用・`_on_binary` ガードを active/playback/id へ・`_finish_stream` は engine 残す・close で engine.close・reconnect で engine 維持）
- `client/config.py`（`OutputConfig.prime_ms` 既定 0）
- `client.toml`（`[output].prime_ms = 0`＋コメント整理）
- `client/app.py`（必要なら output device 変更時 `ws` 経由で engine set_device。最小は据え置き）
- `tests/test_playback_engine.py`（**新規**）・`tests/test_ws_client.py`（`engine_factory`/FakeEngine へ移行）
- `client/audio/sink.py` は**残置（legacy）**。

## 6. テスト/検証
- **ユニット**（GPT-SoVITS/実マイク/実デバイス不要）: §4。fake stream/resampler/loop 注入。keepalive・warm barrier・seq 破棄・協調 abort・reopen・例外復旧・満杯・log/warm の call_soon_threadsafe。
- **実機検証（必須）**: §3.5。null sink 録音で頭欠けゼロ＋cancel→無音時間。tone-from-0 と実 GPT 発話。初回/2回目/idle後/device変更。`prime_ms=0` vs `500` 比較。
- AST境界・py_compile・全 pytest green。**新規依存ゼロ**。

## 7. リスク・留意点
- **初回 warm 保証（critical#1）**: connect 後 `prewarm`＋`warm_min_ms` barrier。これが無いと初回頭欠け再発。device 変更後初回は reopen＋barrier、または「変更後初回のみ短い再 warm 許容」を明記。
- **abort SLO（critical#2）**: 協調 abort（write を ≤20ms 分割＋seq 変化チェック）。`stream.abort()` を使わない（warm 維持優先）。SLO は ≤ max_write_ms + device latency。検証で cancel→無音時間を実測。
- **単一所有 worker（critical#3）**: `_cur/_resampler/_carry/_stream/_play_seq` は worker のみ。main は `_q`/`_seq`/起動終了フラグのみ。seq で古い発話の item を破棄（queue drain しない）。`_BEGIN` で必ず carry/resampler reset（should#3,#8）。
- **WSClient `_engine`/`_playback` 分離（critical#4）**: `_on_binary` は active/playback/cancelling/id で判定（engine 有無で判定しない）。`_finish_stream` は playback 状態のみ消す。close で engine.close、reconnect では engine 維持（warm）。
- **worker 例外復旧（critical#5）**: finally で lock 下に `_thread/_stream=None,_started=False,_closing=False` 復旧。次 prewarm/begin で再起動。例外後復旧ユニット必須。
- **keepalive のブロック/SUSPEND（should#1）**: keepalive write のブロック時間をログ化。実機で idle 後も sink が active か確認。SUSPEND するなら keepalive 間隔を詰める/PipeWire 設定。
- **mono 固定（should#7/#12）**: MVP は mono のみ（多ch は拒否ログ）。channels 変更は将来 reopen 条件。
- **電力/device 占有**: keepalive 常時で device を常に active に保つ。将来「N 秒無発話で auto-close（次発話で再 warm or prime）」を省電力オプションに。
- **prime 三重状態（nit#3）**: engine 既定=prime不要(0)、legacy StreamingPlayer の prime は据え置き、config/toml は 0＋コメントで優先順位明記。
- **検証の盲点を繰り返さない**: 頭欠けは fake では見逃した。X3 は実 OutputStream を実 sink で鳴らし録音して検証する（§3.5 必須）。fake ユニットは worker ロジック（keepalive/seq/協調abort/復旧）に限定。
- **後方互換最重要**: `PlaybackSink`/`StreamingPlayer`/legacy テスト残置。`engine_factory` 新設で `sink_factory` 契約を壊さない。全既存テスト green を必須条件。
- **新規依存ゼロ**。
