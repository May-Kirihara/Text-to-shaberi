# IMPL-SPEAKSTREAM Phase B — GUI 配線（ライブマイク → speak_stream）

> 親計画: `docs/IMPL-SPEAKSTREAM.md`（§9 確定仕様）/ 土台: `docs/IMPL-SPEAKSTREAM-PHASEA.md`（実装済・commit `50cd18d`）。
> 目的: Phase A の土台（StreamSession / StreamingVadSegmenter / WSClient stream API）を **GUI に配線**し、ユーザが実際に「喋りながら順次TTS」を体感できるようにする。
> 対象基準コミット: `50cd18d`（Phase A merged to main）。

## 0. 確定済みの設計判断（ユーザ回答 2026-06-04）

1. **メイン画面にモード切替トグル**を置く。「発話単位（既存 VAD→`utter`）」と「ストリーミング（live mic→`speak_stream`）」を**併存**。既存 utter 経路は**温存**（後方互換）。スマホ化時もモード概念を流用。
2. 進め方は通常フロー（本計画 → codex レビュー → v2 反映 → GLM 小委託実装 → pytest ＋実機スモーク）。

## 1. 全体像

```
[mic スレッド]                              [loop スレッド]                         [WSClient]
MicCapture(stream_mode)                     StreamBridge(consumer task)
 webrtcvad→StreamingVadSegmenter            asyncio.Queue を直列消費
   feed()→events ──call_soon_threadsafe──▶  StreamStart  → await ws.start_stream()
                                            SubSegment(i)→ await ws.send_stream_segment(i, pcm)
                                            StreamComplete→ await ws.end_stream()
```

- **順序保証の肝**: mic イベントは `call_soon_threadsafe` で loop に渡り、**単一の consumer task が asyncio.Queue を1件ずつ await 処理**する。`ensure_future` を毎イベント撒くと start より先に segment が走る競合があるため、**Queue＋単一 consumer** で直列化する。
- **busy 中も mic を止めない**（§9.7）。stream モードでは `_on_state` の busy→`mic.pause()` を**しない**（サーバが順序保持）。busy は従来どおり**単発 speak/送信ボタンの無効化**には使う。
- **ケイパビリティ交渉**: `ready.features.speak_stream` が False のサーバではトグルを**無効化**し発話単位へ固定（WSClient.start_stream も None を返す二重防御）。

## 2. 変更点（ファイル別）

### 2.1 `client/config.py`
- `VADConfig` に **streaming サブセグメント用パラメータ**を追加（既存フィールドは不変）:
  ```python
  seg_silence_ms: int = 250   # 短無音=サブseg確定
  end_silence_ms: int = 500   # 長無音=stream終端
  seg_min_ms: int = 400       # これ未満の極小片は連結
  seg_max_ms: int = 2500      # ポーズ無しでも切る
  ```
- `MicConfig` に **`stream_mode: bool = False`**（最後に選んだモードを永続化する既定値）。
- `load_client_config`: `mic_raw.get("stream_mode", False)` を読む。`vad` は既存 `_only(vad_raw, VADConfig)` で seg_* も自動取り込み（VADConfig に足すだけ）。

### 2.2 `client/audio/mic.py`（MicCapture に stream モード追加）
- `__init__` に **`stream_mode: bool = False`** と **`on_event: Callable[[object], None] | None = None`** を追加。
- segmenter 構築: `segmenter_factory` が無く `stream_mode=True` のとき **`StreamingVadSegmenter`** を `vad_config` から構築:
  ```python
  StreamingVadSegmenter(
      sample_rate=sample_rate, frame_ms=frame_ms,
      aggressiveness=vad.aggressiveness,
      seg_silence_ms=vad.seg_silence_ms, end_silence_ms=vad.end_silence_ms,
      seg_min_ms=vad.seg_min_ms, seg_max_ms=vad.seg_max_ms,
      speech_pad_ms=vad.speech_pad_ms,
      gate=preprocess.gate, gate_threshold=preprocess.gate_threshold,
  )
  ```
  `stream_mode=False` のときは従来 `VadSegmenter`（不変）。
- `_run`: stream モードでは `for ev in self._segmenter.feed(pcm): self._emit_event(ev)`。ループ終了時（stop）は `for ev in self._segmenter.flush(): self._emit_event(ev)`（StreamComplete を確実に出す）。utterance モードは**従来コードのまま**（`_emit_segment` / `flush()->bytes|None`）。分岐は `if self._stream_mode:` で。
- `_emit_event(ev)`: `if self._loop and self._on_event: self._loop.call_soon_threadsafe(self._on_event, ev)`。
- pause/resume は stream モードでは app から呼ばれない（busy で止めない）。`reset()` は `StreamingVadSegmenter.reset()` が存在するので安全。

### 2.3 `client/stream_bridge.py`（新規・テスト可能な配線ユニット）
mic イベント → ws stream 呼び出しを**直列**に橋渡しする小クラス。**loop スレッド専用**（`handle_event` は `call_soon_threadsafe` 経由で loop 上で呼ばれる前提）。

```python
class StreamBridge:
    def __init__(self, ws, *, on_log=None, loop=None): ...
    def start(self) -> None:           # consumer task を起動（idempotent）
    def stop(self) -> None:            # consumer task を cancel・queue クリア・進行中stream を ws.cancel
    def handle_event(self, ev) -> None:  # StreamStart/SubSegment/StreamComplete を queue.put_nowait
    async def _consume(self) -> None:
        # while: ev = await queue.get()
        #   StreamStart   → sid = await ws.start_stream(auto_tts=True)
        #                   未対応(None)なら _active=False にして以後その stream の seg を捨てる＋警告ログ(1回)
        #   SubSegment    → _active なら await ws.send_stream_segment(ev.index, ev.audio)
        #   StreamComplete→ _active なら await ws.end_stream(); _active=False
```
- **fallback**: `start_stream()` が None（サーバ未対応）の場合、その stream の SubSegment を捨て、`StreamComplete` まで読み飛ばす。警告は1回だけ。
- **stop()**: mic OFF / モード切替 / 設定変更 / アプリ終了で呼ぶ。進行中なら `await ws.cancel()`（stream_stopped 受領）。

> 設計理由: `StreamBridge` を独立クラスにすることで **fake ws を注入した単体テスト**で「start→segment×N→end の順序・index・fallback」を pytest 検証できる（GUI 非依存）。

### 2.4 `client/app.py`（run_gui の配線）
- runtime 状態: `mic_stream_mode = [cfg.mic.stream_mode]`、`stream_bridge = StreamBridge(ws, on_log=window.log, loop=loop)`、`stream_supported = [False]`。
- **`_on_state`**:
  - `ready`: `has_speak_stream = payload.features.get("speak_stream")` を見て `stream_supported[0]=bool(...)`。`window.set_stream_mode_available(stream_supported[0])`。未対応かつ現在 stream モードなら発話単位へ降格（`mic_stream_mode[0]=False`, `window.set_stream_mode(False)`、稼働中なら mic 再起動）。
  - `busy`: **stream モードのときは mic.pause を呼ばない**（`if not mic_stream_mode[0] and ...: mic.pause()`）。発話単位は従来どおり。`window.set_busy(True)` は両モードで呼ぶ（送信/停止ボタン状態）。
  - `idle`: 同様に **stream モードでは resume しない**（そもそも pause していない）。発話単位は従来どおり。
- **`on_mic_toggle(active)`**: mic 構築時に**現在のモードで分岐**:
  - stream: `MicCapture(..., stream_mode=True, on_event=stream_bridge.handle_event)` を作り、`stream_bridge.start()` してから `mic.start()`。
  - utterance: 従来 `MicCapture(..., on_segment=on_segment)`。
  - OFF 時: `mic.stop()`、stream モードなら `stream_bridge.stop()`。
- **`on_mode_toggle(stream: bool)`**（新規・window から）:
  - `mic_stream_mode[0]=stream`、toml に `{"mic": {"stream_mode": stream}}` を `update_client_toml` で永続化、`cfg.mic.stream_mode=stream`。
  - mic 稼働中なら**現モードで再起動**（`mic_reconfiguring` ガードで一旦 stop→`on_mic_toggle(True)`）。
- **`on_settings`**: 既存に加え `vad_vals` から `seg_silence_ms/end_silence_ms/seg_min_ms/seg_max_ms` を `cfg.mic.vad` に反映。設定変更後の mic 再起動は既存ロジック（`if mic_enabled_by_user[0]: on_mic_toggle(True)`）が現モードで張り直す。
- **`_shutdown`**: `stream_bridge.stop()` を追加（mic.stop の後）。
- 配線登録: `window.set_on_mode_toggle(on_mode_toggle)`、初期反映 `window.set_stream_mode(mic_stream_mode[0])`、`window.set_stream_mode_available(False)`（ready で更新）。

### 2.5 `client/ui/main_window.py`（モードトグル）
- mic ボタン横に **checkable QPushButton**（`self.mode_button`）を追加。OFF(unchecked)=「発話単位」、ON(checked)=「ストリーミング」。`toggled→_handle_mode_toggle→self._on_mode_toggle(checked)`、ラベル/ツールチップを状態反映。
- API: `set_on_mode_toggle(cb)` / `set_stream_mode(bool)`（checked＋ラベル更新、シグナル発火抑止のため `blockSignals`）/ `set_stream_mode_available(bool)`（enable/disable＋未対応時ツールチップ「サーバが未対応」）。
- 既存ボタン配置（input_row）に `self.mode_button` を mic_button の前に挿入。

### 2.6 `client/ui/settings_dialog.py` + `values()`
- マイク QGroupBox に streaming 用 QSpinBox を追加: `seg_silence_ms(100-2000)`, `end_silence_ms(100-3000)`, `seg_min_ms(50-2000)`, `seg_max_ms(500-10000)`。ラベルは「サブseg無音ms / 発話末無音ms / 最小seg ms / 最大seg ms」。`mic.vad` から初期値。
- 「既定でストリーミング」QCheckBox（`mic.stream_mode`）も追加（任意）。
- `values()`: `"mic.vad"` に seg_* を追加、`"mic"` に `stream_mode` を追加。`update_client_toml` はドット section と新規 section/key 追記に対応済（`settings_store.py`）。

## 3. テスト（pytest・GPU/マイク/GUI不要）

1. **`tests/test_mic_stream_mode.py`（新規）**: `MicCapture(stream_mode=True, stream_factory=Fake, on_event=rec, loop=Fake)` に**実 StreamingVadSegmenter**（または fake segmenter）を使い、合成フレーム列を流して `on_event` が `StreamStart→SubSegment(index昇順)→StreamComplete` を受けることを検証。`stop()` の flush で StreamComplete が出ることも。`call_soon_threadsafe` は即時実行する FakeLoop を注入（既存 `tests/test_mic_capture.py` の流儀を踏襲）。
2. **`tests/test_stream_bridge.py`（新規）**: fake ws（`start_stream`/`send_stream_segment`/`end_stream` を記録、`start_stream` は sid を返す async）。`handle_event` に `StreamStart, SubSegment(0,b"a"), SubSegment(1,b"b"), StreamComplete` を渡し loop を回し、**呼び出し順と引数**（index 0,1・audio・end 1回）を assert。`start_stream`→None の fallback（segment 捨て・end 呼ばず・警告1回）も。冪等 start/stop も。
3. **`tests/test_client_config.py`（既存 or 新規）**: `seg_silence_ms` 等と `stream_mode` が toml から読める。
4. **`tests/test_settings_dialog.py`（既存に追記）**: `values()["mic.vad"]` に seg_* が、`values()["mic"]` に `stream_mode` が入る。
5. **`tests/test_main_window.py`（既存に追記）**: `set_on_mode_toggle` 登録 → トグルで cb(checked) 発火。`set_stream_mode(True)` でラベル「ストリーミング」・checked。`set_stream_mode_available(False)` で disabled。

## 4. 実機スモーク検証（GUI バグ捕捉・pytest をすり抜ける層）

GUI 専用バグ（qasync `aboutToQuit` 級）と Phase A+B 疎通を**実地**で確認する。委託後にこちらで実行する。

- **(S1) run_gui 構築スモーク（offscreen）**: `QT_QPA_PLATFORM=offscreen` で `run_gui` の配線構築（MainWindow 生成・全 set_on_* 接続・`on_mode_toggle(True)`／`on_mic_toggle` のシグナル経路）を**短時間起動→即終了**し、`AttributeError`/例外ゼロを確認（実マイク無しでも mic 構築前に落ちないことを見る。mic.start は実デバイス必要なので toggle は呼ぶがデバイス例外は許容しログ確認）。
- **(S2) headless mic→ref.wav→stream e2e**: 実 `StreamingVadSegmenter`(実 webrtcvad) に `assets/ref.wav` の 16k mono PCM を流して **StreamStart/SubSegment/StreamComplete を生成** → 実 `WSClient.start_stream/send_stream_segment/end_stream` を**実サーバ**（ローカル uvicorn か docker のサーバ）へ。`transcript`(index 昇順) と音声フレーム受領、`stream_stopped` not stuck を確認（[[glm-delegation-uv-lock-contention]] の headless パターンの stream 版）。再生は fake sink/engine で bytes 受領＝音響デバイス不要。

## 8. codex レビュー反映（v2 確定仕様・本節が §2〜§3 の該当部を上書き）

codex(gpt-5.5, read-only) を実コードで裏取りのうえ反映。判定は「下記反映後に Phase B 着手可」。HIGH×4/MED×4/LOW×1。

### 8.1 HIGH①③＋MED② — StreamBridge を「入力/wire を分離した stop/drain 状態機械」にする

bridge の状態を **2軸**で持つ:
- `_input_open: bool` … mic 入力側で StreamStart..StreamComplete の間（このマイク発話を受付中）。
- `_ws_active: bool` … wire 上で stream を開始済みでまだ idle 確定していない（`start_stream` 成功〜`AudioEnd`/`StreamStopped`→WSClient idle まで。**`end_stream()` 送信後も True のまま**＝サーバはまだ生成中かもしれない: MED②）。

```python
class StreamBridge:
    def __init__(self, ws, *, on_log=None, loop=None, queue_max=64): ...
    # app から呼ぶ
    def set_supported(self, ok: bool): self._supported = ok
    def notify_ws_busy(self):  self._ws_idle.clear()           # _on_state("busy")
    def notify_ws_idle(self):  self._ws_active=False; self._ws_idle.set()  # _on_state("idle")
    def start(self):  # consumer task 起動（idempotent）。_ws_idle は初期 set。
    def handle_event(self, ev):  # loop上。_closing なら drop。queue 満杯は最古を捨て log(1回)
        self._queue.put_nowait(ev)
    async def close(self):  # mic OFF/mode切替/降格/shutdown で必ず await
        self._closing = True
        self._queue.put_nowait(_STOP)
        try: await asyncio.wait_for(self._consumer, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            self._consumer.cancel()
            if self._ws_active: 
                try: await self.ws.cancel()
                except Exception: pass
        self._ws_active=False; self._input_open=False

    async def _consume(self):
        while True:
            ev = await self._queue.get()
            if ev is _STOP:
                if self._ws_active:                 # 進行中stream を確実に終端
                    await self.ws.cancel()           # → stream_stopped
                return
            if isinstance(ev, StreamStart):
                if self._closing: continue           # shutdown 中は新規stream を開かない
                self._input_open=True; self._failed=False; self._fallback_buf=bytearray()
                if not self._supported:              # 未対応＝utter フォールバック（buffer）
                    self._mode="fallback"; continue
                # 直前 stream の再生/生成が終わるまで待つ（wire は単一 active）
                try: await asyncio.wait_for(self._ws_idle.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    self._failed=True; self._log("[stream] 前発話の終了待ちタイムアウト→今回の発話を破棄"); continue
                sid = await self.ws.start_stream(auto_tts=True)
                if sid is None:                      # busy/接続失敗等
                    self._failed=True; self._log("[stream] start不可→今回の発話を破棄"); continue
                self._ws_active=True; self._mode="stream"
            elif isinstance(ev, SubSegment):
                if self._failed: continue
                if self._mode=="fallback": self._fallback_buf += ev.audio; continue
                if self._ws_active: await self.ws.send_stream_segment(ev.index, ev.audio)
            elif isinstance(ev, StreamComplete):
                if self._mode=="fallback" and self._fallback_buf:
                    await self.ws.utter(bytes(self._fallback_buf), auto_tts=True)  # 未対応サーバへの降格
                elif self._ws_active:
                    await self.ws.end_stream()       # _ws_active は idle 通知まで True
                self._input_open=False; self._mode=None; self._fallback_buf=bytearray()
```
- **`send_stream_segment` は WSClient 側で seg 全体を `_send_lock` で原子送信**（既存実装）。よって `_STOP` は「現イベントを処理し切ってから」読まれる＝半端な seg_begin/seg_end を作らない（task.cancel は timeout 時の最終手段のみ）。
- **HIGH③解消**: 自然終端は feed() の `StreamComplete`→`end_stream`。**mic OFF/モード切替などの強制終了は flush に依存せず `bridge.close()` が `ws.cancel()` で確実に終端**（最後のサブseg音声の取りこぼしは許容）。`MicCapture` stream flush の `StreamComplete` は「来れば優雅に end、来なければ close が cancel」の二重保険。
- **MED②解消**: `_ws_active` は `end_stream()` 後も True。idle 通知（`notify_ws_idle`）で False 化。よって end 後の mic OFF でも close() が必要なら cancel できる。

### 8.2 HIGH② — `start_stream→None` を無音 drop にしない（理由別）

- **未対応**（`features.speak_stream` False）: bridge を `set_supported(False)`。StreamStart で fallback モード＝SubSegment を貯め、StreamComplete で **1本の `ws.utter()`** に落とす（Phase A 仕様「未対応は従来 utter フォールバック」を満たす）。トグルは元々無効化しているので通常到達しないが二重防御。
- **busy（前 stream 進行中）**: wire は単一 active のため、**`_ws_idle` を待ってから start**（最大 8s）。待てたら start、タイムアウト/それでも None なら **その発話を破棄＋ログ**（既存 utterance モードの busy-skip と同じ確定的挙動）。「サーバの順序保持は1 stream 内のみ・**発話間は bridge が直列化**」を明記。
- **WSClient.start_stream の堅牢化（Phase A コードの小修正）**: `_active_id` をセットした後の送信を `try/except BaseException: self._finish_stream(); raise` で包み、**CancelledError でも `_active_id` を残さない**（HIGH① の stuck 防止）。

### 8.3 HIGH④ — UIモードと実capture modeを分離

- app は **`active_mic_mode: "utterance"|"stream"|None`**（実際に動いている MicCapture のモード）を `mic_stream_mode`（UI希望値）とは別に持つ。
- `_on_state("busy"/"idle")` の mic pause/resume は **`active_mic_mode=="utterance"` のときだけ**行う（stream は pause しない）。`mic_stream_mode` では判定しない。
- モード/設定変更の手順を固定: `mic_reconfiguring=True` → 旧 `bridge.close()`＋`mic.stop()` 完了 → **`active_mic_mode=None`** → 新 capture 構築 → `active_mic_mode=新` → `mic_reconfiguring=False`。`on_mode_toggle` で先にフラグだけ進めない。

### 8.4 MED① — ケイパビリティ：ready 前は無効、降格手順を明文化

- 起動直後は **mic ボタンとモードトグルを disabled**（`window.set_mic_enabled(False)`＋`set_stream_mode_available(False)`）。`on_mic_toggle(True)` 先頭にも接続/`stream_supported` guard。
- `ready` 毎に `auto_tts`/`speak_stream` を反映（再接続で変化し得る前提）。
- **降格（speak_stream: true→false）**: 稼働中 stream を `await bridge.close()`（cancel）→ `active_mic_mode=None` → モードトグルを発話単位に固定・disabled → mic ON のままなら utterance で再起動。
- **昇格（false→true）**: トグルを enable（自動でモード変更はしない。ユーザ操作で切替）。

### 8.5 MED③ — `seg_silence<end_silence` 不変条件を assert 任せにしない

- `load_client_config` で **クランプ**: `end_silence_ms = max(end_silence_ms, seg_silence_ms + 2*frame_ms)`、`seg_max_ms = max(seg_max_ms, seg_min_ms)`、各 ms は下限1。補正時は警告ログ。
- `StreamingVadSegmenter.__init__` の `assert` は `if not (seg_silence_frames < end_silence_frames): raise ValueError(...)` に格上げ（-O で消えない）。
- 設定ダイアログは `end_silence_ms` の minimum を `seg_silence_ms` 連動にするか、保存時クランプ（load 側クランプがあるので最低限そちらで担保）。

### 8.6 MED④ — 追加テスト（§3 を拡張）

- `start_stream()` await 中に `bridge.close()` → `_active_id` が残らない（WSClient 堅牢化込み）。
- `send_stream_segment()` 進行中の close → seg が半端にならない（`_STOP` は現イベント完了後に読まれる）。
- `mic.stop()` の flush `StreamComplete` と `close()` が競合しても **end_stream か cancel が必ず1回**。
- `start_stream→None` 理由別: 未対応→utter フォールバック1回 / busy→idle待ち後 start / タイムアウト→破棄＋ログ。
- モード切替中の busy/idle で旧 capture に誤 pause/resume が入らない（`active_mic_mode` 分岐）。
- ready 前 mic ON 不可、再接続で `speak_stream` true→false→true の降格/昇格。

### 8.7 LOW — transcript 二重ログ解消

- WSClient の **stream_mode 分岐では `_log("🎤→ ...")` を出さず `_state("transcript", ...)` のみ**にする（app 側 `_on_state("transcript")` が1回ログ）。Phase A 実装の該当2行を調整。

### 8.8 総合判定

Queue＋単一 consumer の方向は妥当。**8.1〜8.5 を反映後に Phase B 着手可**。本番バグの主戦場は「consumer をどう止めるか（close の drain/cancel）」と「発話間 stream をどう直列化するか」。

## 5. スコープ外（Phase C 以降）

- `prev_tail` 句文脈の実利用（STT 精度）。
- prosody 連続化。
- **複数TTS(N≥2)**（`_gen_output_worker` の gen プール＋reorder 化）。
- 真の再デコード型ストリーミング STT。

## 6. リスク / codex 観点

| 論点 | 内容 |
|---|---|
| mic→ws 順序 | Queue＋単一 consumer で直列化（ensure_future 撒きの競合回避）。start→seg→end の厳密順序を test_stream_bridge で実証 |
| busy 中 mic 継続 | stream モードで pause しない。発話単位の既存 pause/resume と分岐が交錯しないか（mic_stream_mode フラグ1点で分岐） |
| モード切替の再起動 | mic 稼働中のトグル/設定変更で MicCapture を停止→現モードで再構築。`mic_reconfiguring` ガードと busy 自動pauseの相互作用 |
| ケイパビリティ降格 | features.speak_stream=False のサーバでトグル無効化＋発話単位固定。再接続で features が変わる場合の再評価（ready 毎に判定） |
| StreamComplete 漏れ | mic stop 時の flush で必ず StreamComplete→end_stream。途中切断時に stream が開いたままにならないか（_shutdown / disconnected で bridge.stop） |
| GUI バグ | pytest は run_gui を通らない。S1/S2 実機スモーク必須 |
| stop 競合 | bridge.stop() と consumer の進行中 await（start/send/end）の協調キャンセル。ws.cancel との二重 |

## 7. 作業順

1. 本計画 → codex(read-only) レビュー → v2 反映（特に §2.3 順序保証・§2.4 busy分岐・§4 スモーク）。
2. GLM 小委託（1ターン=1ファイル: config → mic → stream_bridge → app → main_window → settings_dialog → tests）。各ターン自己検証コマンド＋短い推論。
3. `uv run pytest tests/ -q` 全 pass（既存不変）。
4. S1/S2 実機スモークをこちらで実行。
5. branch → commit → `git merge --ff-only` main。
