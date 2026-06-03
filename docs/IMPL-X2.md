# X2 実装計画書 v1: 仕上げ（再接続 / 全設定UI / マイクpause / 計測 / エラー処理）

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2)。S1/C1/S2/C2/X1 で「テキスト/マイク→STT→auto_tts→opus再生」＋token/wss は完成。X2 は **日常運用に耐える仕上げ**: 自動再接続・全設定のGUI化(#13内包)・マイクの適切な一時停止(ハーフデュプレックス改善)・計測の可視化・エラー処理の一貫化。
> フロー: 本書 → codexレビュー(`docs/REVIEW-codex-X2.md`) → v2改訂 → GLM委託 → 私が検証/統合 → `docs/CODE-REVIEW-X2.md`。**この仕様の通りに実装すること。**
> 前提: X1完了（`f208772`）。`text-to-shaberi-server`(:8765) 稼働。**新規依存は追加しない**（PySide6/qasync/websockets/sounddevice/soxr/opuslib/webrtcvad は既存）。

## 1. 目的・スコープ
- スコープ内（5本柱）:
  1. **自動再接続（指数バックオフ）** `ws_client.py`: 予期せぬ切断で自動再接続（ユーザ close とは区別）。`on_state("reconnecting", attempt)`／成功で `connected`。UI に再接続状態表示。
  2. **マイクの適切な一時停止（ハーフデュプレックス改善）** `mic.py`+`app.py`: 再生/送信中（busy）は **MicCapture を pause**（フレーム破棄＋segmenter reset）、idle で resume。C2 の「busy-skip ログ」を実停止に置換し、TTS→マイク フィードバックと無駄な VAD を抑止。
  3. **全設定 GUI（#13 内包）** `settings_dialog.py`+`settings_store.py`: サーバ（address/token/codec/tls_insecure/tls_cafile）・マイク（device/VAD aggressiveness/min_silence_ms/speech_pad_ms/gate）・出力（device）をダイアログで編集→`client.toml` 保存→必要に応じ再接続/MicCapture 再生成。**`update_client_toml` を int/bool 対応に拡張**。
  4. **計測の可視化** `ws_client.py`+`main_window.py`: TTFA・STT レイテンシ・採用コーデック・接続状態を **ステータス行**に常時表示（既存のログ出力に加え構造化）。
  5. **エラー処理の一貫化** 全体: 接続失敗/サーバ error/デバイス失敗をユーザ可視メッセージ＋状態表示に統一。再接続インジケータ。
- スコープ外（明示）:
  - 本格 **AEC（音響エコーキャンセル）/バージイン**（pause で代替。ヘッドフォン運用前提）。
  - **デバイスホットスワップ**（抜き差し自動検出）。設定変更時の手動再生成のみ。
  - マルチプロファイル同時/プロファイル編集UI・i18n・テーマ。STT バックエンド切替UI。
  - Opus アップリンク（X1 スコープ外のまま）。

## 2. 既存資産
- `client/ws_client.py`: `connect`/`reconnect(address,token)`/`close`/`speak`/`utter`/`cancel`、`_recv_loop`（`ConnectionClosed`→`on_state("disconnected")`＋active 中断）、`on_state` イベント（ready/connected/busy/idle/disconnected/error/transcript）、`_t_send`/TTFA ログ、`_ready`（codec/limits）。**本書で自動再接続・計測 state を追加**。
- `client/audio/mic.py`: `MicCapture`（worker ブロッキング read→VadSegmenter→`on_segment`、`start`/`stop`、`call_soon_threadsafe`）。**pause/resume を追加**。
- `client/audio/devices.py`: `list_input_devices()`/`list_output_devices()`/`resolve_device(name|idx|'default', kind)`。**設定UIのデバイス列挙に流用**。
- `client/ui/main_window.py`: 出力 combo・接続ラベル(`set_connection`)・🎤トグル(`set_mic_active`/`set_mic_enabled`)・ログ・歯車(`set_on_settings`)・`set_busy`。**ステータス行・再接続表示を追加**。
- `client/ui/settings_dialog.py`: 最小版（address/token/profile）。**全項目へ拡張**。
- `client/settings_store.py`: `update_client_toml(path, dict[str,dict[str,str]])`（**scalar 文字列のみ**＝`_toml_str` が全部 quote）。**int/bool 対応に拡張**。
- `client/config.py`: `ClientConfig`（server.address/token/codec/tls_insecure/tls_cafile/token_in_query、output.device、mic.device/sample_rate/channels/preprocess(gate/denoise/gate_threshold)/vad(engine/min_silence_ms/speech_pad_ms/aggressiveness/frame_ms/min_speech_ms/max_speech_ms)、profile.active_id）。
- `client/app.py`: WSClient/MicCapture 配線・`_on_state`・`on_mic_toggle`/`on_segment`（busy-skip ログ）・`on_settings`（最小保存→reconnect）・`_shutdown`。

## 3. 設計

### 3.1 自動再接続（指数バックオフ）`ws_client.py`
- **状態**: `self._user_closing: bool=False`（`close()` で True）、`self._reconnect_task: asyncio.Task|None`、`self._reconnect_attempt:int=0`、`self._auto_reconnect:bool=True`（注入可・テスト用に False）。
- **トリガ**: `_recv_loop` の `ConnectionClosed`/例外で、**`_user_closing` でなく `_auto_reconnect`** のとき:
  - active stream があれば従来通り `_sink.abort()`＋`_finish_stream()`（中断）。
  - `on_state("disconnected")` 後、`_schedule_reconnect()`。
- **`_schedule_reconnect()`**: 既存 `_reconnect_task` が生きていれば何もしない。`self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())`。
- **`_reconnect_loop()`**:
  ```
  while not self._user_closing:
      delay = min(base * 2**attempt, max_delay) + jitter   # base=0.5, max=30
      self._reconnect_attempt += 1
      on_state("reconnecting", attempt)
      await asyncio.sleep(delay)
      if self._user_closing: break
      ok = await self.connect()    # connect() 成功で _recv_loop 再起動・ready/connected 発火
      if ok: self._reconnect_attempt = 0; return
  ```
  - `connect()` は内部で `_ws`/`_recv_task` を作る。再接続時に古い `_recv_task` は既に終了（ConnectionClosed で抜けた）。多重起動防止: `connect()` 冒頭で既存 `_recv_task` が生きてたら return False か無視（現状 connect は無条件に作るので、`_reconnect_loop` からのみ呼ぶ前提で可。多重防止のため `_connecting` ガードを足す）。
- **`close()`**: `self._user_closing=True`、`_reconnect_task` cancel、以降従来の teardown。再接続を止める。
- **`reconnect(address,token)`（設定変更時）**: `_user_closing` を立てずに `close()` 相当の teardown→`connect()`。自動再接続ループと競合しないよう、明示 reconnect 中は `_auto_reconnect` を一時停止 or `_reconnect_task` を cancel してから。
- **speak/utter の自動接続**: 既存（未接続なら connect）。再接続ループ作動中に speak が来たら busy/未接続で弾くか、ループ完了を待たずローカル接続試行（MVP は「未接続なら接続失敗ログ」で可。再接続はバックグラウンドで進む）。
- **ジッタ**: `delay += random.uniform(0, base)` 程度（thundering herd 回避。単一クライアントなので必須ではないが軽く）。`random` は標準ライブラリ。
- **パラメタ**: `client.toml [server]` に `reconnect=true`・`reconnect_max_delay=30`（設定化）。

### 3.2 マイク pause/resume（ハーフデュプレックス改善）`mic.py`+`app.py`
- **`MicCapture`**: `self._paused = threading.Event()`（注: Event は「セット=pause」運用）。`pause()`/`resume()`/`@property paused`。
  - worker `_run`: `data = read(...)` は続ける（デバイスのリングバッファを溜めない＝オーバーフロー防止）が、**`_paused.is_set()` の間は segmenter に流さず破棄**。pause へ遷移した瞬間に **`segmenter.reset()` を1回**（中途半端なセグメントを捨てる）。resume では reset 不要（idle 状態から再開）。
  - `pause()`: `if not self._paused.is_set(): self._paused.set(); <reset フラグを立て worker が次ループで reset>`。`resume()`: `_paused.clear()`。冪等。
  - スレッド安全: `_paused` は Event。segmenter reset は worker スレッド内で行う（segmenter は worker 専有なので main から触らない）。pause 要求は Event＋`_reset_pending` フラグ（main セット→worker クリア）で伝える。
- **`app.py` 配線**: `_on_state`:
  - `"busy"` → mic があれば `mic.pause()`（再生/送信中はマイク停止）。
  - `"idle"` → mic がトグル ON のままなら `mic.resume()`。
  - C2 の `on_segment` の busy-skip は **保険として残す**（pause 反映前に来た最後の1セグメント対策）が、通常は pause でセグメント自体が来ない。
- これで「自分の TTS をマイクが拾って再 STT」ループを実質遮断（ヘッドフォン無しでも実用に近づく）。AEC は将来。

### 3.3 全設定 GUI `settings_dialog.py`（#13 内包）
- `SettingsDialog(parent, cfg, profiles, input_devices, output_devices)` に拡張。`QFormLayout` を **QGroupBox 3つ**（サーバ／マイク／出力）に整理（タブでも可）。
  - **サーバ**: address(`QLineEdit`)・token(`QLineEdit` Password)・codec(`QComboBox`: opus/pcm)・tls_insecure(`QCheckBox`)・tls_cafile(`QLineEdit`＋参照ボタンは任意)。
  - **マイク**: device(`QComboBox`: 「既定」＋ `list_input_devices()`)・aggressiveness(`QSpinBox` 0-3)・min_silence_ms(`QSpinBox`)・speech_pad_ms(`QSpinBox`)・gate(`QCheckBox`)・gate_threshold(`QSpinBox` 任意)。
  - **出力**: device(`QComboBox`: 既定/TTS_Virtual/`list_output_devices()`)。現状 main の combo と二重になるので、**設定の output.device を既定選択に反映**（main combo は即時切替用に残置）。
  - profile(`QComboBox`)は従来通りサーバ群に。
- `values() -> dict`: **型付き**で返す（int は int、bool は bool、str は str）。例 `{"server":{"address":str,"token":str,"codec":str,"tls_insecure":bool,"tls_cafile":str},"mic":{"device":str,"vad":{...},"preprocess":{...}},"output":{"device":str},"profile":{"active_id":str}}`。ネストは dialog 側でフラット化して app に渡してもよい。
- **デバイス列挙失敗**（portaudio 不在）は空リストで握りつぶし「既定」のみ（C1 流儀）。

### 3.4 `settings_store.py` の型対応
- `update_client_toml(path, updates)` の `updates` 値を **str|int|bool|float** 許容に拡張。
- `_toml_value(v)`: `bool`→`"true"`/`"false"`（**bool は int より先に判定**＝`isinstance(v,bool)` を先に）、`int`/`float`→`str(v)`、`str`→既存 `_toml_str(v)`（quote＋エスケープ）。
- 既存の scalar 文字列パス（C1/X1 が呼ぶ）は後方互換（str はそのまま quote）。**section.key は従来の行指向・コメント保持・原子的書き込み・tomllib 検証を維持**。
- **ネスト table（`[mic.vad]` 等）対応**: 現状 `update_client_toml` は `[section]` 完全一致で `[mic.vad]` も「section名 `mic.vad`」として扱える（`_SECTION_RE` は `[mic.vad]` を group(1)="mic.vad" で拾う）。よって updates のキーを `"mic.vad"`/`"mic.preprocess"`/`"mic"`/`"server"`/`"output"`/`"profile"` のドット付き section 名にすれば既存ロジックで書ける。**app 側で updates をこの形に組み立てる**。

### 3.5 計測の可視化 `ws_client.py`+`main_window.py`
- **WSClient**: 計測を `on_state("metrics", dict)` で通知。
  - `utter`/`speak` 送信時刻 `_t_send`。
  - 初音受信時に **TTFA**（既存ログに加え）。STT 経路は `transcript` 受信時に **STT レイテンシ**（`t_transcript - _t_send`）。
  - `on_state("metrics", {"ttfa_ms":float|None, "stt_ms":float|None, "codec":self._ready.codec, "event":"first_audio"|"transcript"})`。
- **MainWindow**: `set_status(text)` で下部ステータス行（`QLabel` or statusBar）に「接続:✓ codec:opus | STT 1.2s | TTFA 130ms」等を表示。`set_connection` は接続状態のみ、metrics は別ラベル or 結合。再接続中は「再接続中…(n)」。
- ログ（log_view）は従来通り詳細用に残す。ステータス行は要約。

### 3.6 エラー処理の一貫化
- `on_state` を UI 反映の単一窓口に: `error`→ステータス行に赤系＋log、`reconnecting`→「再接続中…」、`disconnected`→「未接続」、`connected`/`ready`→「接続 ✓ codec」。
- 接続失敗（connect False）は `on_state("error", msg)`＋自動再接続ループへ（3.1）。
- デバイス失敗（MicCapture.start 例外・sink 生成失敗）は log＋トグル戻し（C2 既存）＋ステータス通知。
- サーバ terminal error（id==active）は既存（abort+idle+error state）。

### 3.7 設定の即時反映（app.py `on_settings`）
- ダイアログ OK で:
  - `update_client_toml(config_path, updates)`（ドット section 名・型付き）。
  - `cfg` をメモリ更新（server/mic/output/profile）。
  - **サーバ系変更（address/token/codec/tls_*）** → `ws._apply_server_settings(...)`＋`ws.reconnect(...)`（codec_pref/tls も WSClient に反映）。
  - **マイク系変更（device/vad/preprocess）** → MicCapture があれば `stop()`→破棄し、次回トグル ON で新 cfg から再生成（または即再生成）。
  - **出力 device** → main combo 既定選択を更新。
- WSClient に `codec_pref`/`tls_insecure`/`tls_cafile`/`token_in_query` を後から差し替える setter（`_apply_server_settings`）を用意（現状 `__init__` のみ）。

## 4. 受け入れ基準
- [ ] **自動再接続ユニット**（fake WS サーバ・`asyncio.run`）: サーバが接続後に切断すると、クライアントが `reconnecting`→再接続を試み、サーバ復帰で `connected`/`ready` に戻る（バックオフ遅延は短縮注入）。`close()`（user）では再接続しない。`_auto_reconnect=False` 注入で従来挙動。
- [ ] **マイク pause/resume ユニット**（fake stream/segmenter 注入）: `pause()` 後は `on_segment` が発火しない（segmenter に流れない）・pause 遷移で `segmenter.reset()` が1回呼ばれる・`resume()` で再開・冪等。`busy→pause`/`idle→resume` の app 配線（`_on_state`）も検証可能なら。
- [ ] **settings_store 型対応ユニット**: bool→`true`/`false`、int→数値リテラル、str→quote、を書いて tomllib 再読込で型一致。`[mic.vad]` 等ネスト section の既存キー置換・欠損追記・コメント保持・原子的書き込み。**bool が int として誤判定されない**（`True`→`"true"` であって `"1"` でない）。
- [ ] **全設定ダイアログユニット**（QtWidgets・オフスクリーン `QT_QPA_PLATFORM=offscreen`）: cfg から各ウィジェット初期値が入る・`values()` が型付き dict を返す・device 列挙失敗でも「既定」で動く。
- [ ] **計測ユニット**: `utter`→`transcript`→初音 の fake シーケンスで `on_state("metrics", ...)` に `stt_ms`/`ttfa_ms`/`codec` が乗る。
- [ ] **後方互換/境界/コンパイル**: 既存全テスト green（C1/C2/S2/X1）。`update_client_toml` の既存 scalar 呼び出し不変。AST 境界・py_compile。`QT_QPA_PLATFORM=offscreen` で Qt テストが CI 安全。
- [ ] **統合（手動・私がLinuxで）**: サーバ起動→GUI→**サーバ再起動で自動再接続**（ステータスが「再接続中…」→「接続✓」）。🎤ON で発話→**再生中はマイクが止まり**、idle で再開（フィードバックループ無し）。設定ダイアログで mic device/VAD/codec を変更→保存→反映。ステータス行に STT/TTFA/codec 表示。
- [ ] **Windows実機（あなた）**: 再接続・設定UI・マイク pause・ステータス表示。

## 5. 変更/新規ファイル
- `client/ws_client.py`（自動再接続・`_user_closing`/`_reconnect_loop`・`on_state("reconnecting"/"metrics")`・`_apply_server_settings`・計測）
- `client/audio/mic.py`（`pause`/`resume`/`paused`・worker の破棄＋reset）
- `client/ui/settings_dialog.py`（全項目・QGroupBox・型付き `values()`・device 列挙）
- `client/settings_store.py`（`_toml_value` 型対応・ドット section）
- `client/ui/main_window.py`（ステータス行 `set_status`・再接続表示）
- `client/config.py` / `client.toml`（`[server]` reconnect/reconnect_max_delay 追加）
- `client/app.py`（再接続/pause/metrics/全設定保存＆即時反映の配線）
- `tests/test_ws_reconnect.py`（**新規**）・`tests/test_mic_pause.py`（**新規**）・`tests/test_settings_store.py`（型対応追加）・`tests/test_settings_dialog.py`（**新規**・offscreen）・`tests/test_ws_client.py`（metrics 追加）

## 6. テスト/検証
- **ユニット（必須・GPT-SoVITS/実マイク不要）**: 上記 §4。Qt テストは `QT_QPA_PLATFORM=offscreen` を `conftest.py` か各テストで設定（`QApplication` 1個を使い回し）。fake WS サーバ・fake stream/segmenter・一時 toml。
- **統合（手動）**: §4。Linux は私、Windows はあなた。
- AST境界・py_compile・全 pytest green 維持。**新規依存ゼロ**。

## 7. リスク・留意点
- **再接続の競合**: 自動再接続ループと手動 `reconnect()`（設定保存）・`speak`/`utter` の自動接続が同時に `connect()` を呼ぶと多重 `_ws`/`_recv_task`。`_connecting` ガードと「手動操作時は `_reconnect_task` を cancel してから」で直列化。`close()`（user）は `_user_closing` で全停止。
- **マイク pause のスレッド安全**: `_paused`(Event)＋`_reset_pending` フラグは main→worker の一方向。segmenter は worker 専有（main から触らない）。read は pause 中も継続しデバイスのオーバーフローを防ぐ（フレームは破棄）。
- **settings_store の bool/int 判定順**: Python では `bool` は `int` のサブクラス。`isinstance(v, bool)` を `isinstance(v, int)` より**先に**判定（さもないと `True`→`"1"` になる）。
- **ネスト section**: `[mic.vad]` を section 名 `"mic.vad"` として updates に渡せば既存ロジックで書ける（`_SECTION_RE` が拾う）。新規追記時の section 順序は末尾追加で可。
- **Qt テスト**: `QT_QPA_PLATFORM=offscreen` 必須（ヘッドレス）。`QApplication` は 1 プロセス 1 個（フィクスチャで共有）。ウィジェットのみ検証（イベントループは回さない）。
- **設定即時反映の範囲**: サーバ系=再接続、マイク系=MicCapture 再生成、出力=combo 反映。**device ホットスワップ（OS レベルの抜き差し）は対象外**。
- **計測の正確性**: client 側計測（送信→受信）で十分（サーバ内訳は server ログ）。STT レイテンシは「utter 送信→transcript 受信」＝アップロード＋推論を含む近似。
- **後方互換**: `update_client_toml` の既存 str-only 呼び出し（C1/X1）を壊さない。WSClient の既存 API（speak/utter/cancel/connect/close/reconnect）の挙動を保ち、自動再接続は **既定 ON だが注入で OFF** にできる（既存テストが切断で無限再接続しないよう、テストは `_auto_reconnect=False` か短バックオフ＋user close）。
- **新規依存ゼロ**: `random`/`ssl`/`tomllib` は標準ライブラリ。Qt/websockets/sounddevice は既存。
