# X2b 実装計画書 v1: マイクpause ＋ 全設定GUI ＋ 型store ＋ 計測 ＋ エラーUI

> 由来: `docs/IMPL-X2.md`（5本柱）を codex(`docs/REVIEW-codex-X2.md`)が分割勧告。**X2a＝再接続＋接続ライフサイクル**は完了(`b8d3ef3`)。本書 **X2b** はその上に乗る残り全部（mic pause/全設定GUI(#13)/型 settings_store/計測/エラー・状態UI）。
> フロー: 本書 → codexレビューは X2 一括で実施済み（`docs/REVIEW-codex-X2.md` の **critical#5＋should 6,7,8,9,11,12＋nit#15** を本書に反映）→ GLM委託 → 私が検証/統合 → `docs/CODE-REVIEW-X2b.md`。**この仕様の通りに実装すること。**
> 前提: X2a完了（`b8d3ef3`）。`text-to-shaberi-server`(:8765) 稼働。**新規依存ゼロ**（PySide6/sounddevice 既存・`tomllib` 標準）。

## 1. 目的・スコープ
- スコープ内（5本）:
  1. **マイク pause/resume**（`mic.py`+`app.py`）: 再生/送信中（busy）は MicCapture を pause（フレーム破棄＋segmenter reset）、idle で resume。C2 の「busy-skip ログ」を実停止に置換。**pause 後の flush 尾を emit しない**（critical#5/should#6）。
  2. **全設定 GUI**（`settings_dialog.py`・#13 内包）: サーバ/マイク/出力 を QGroupBox で編集。`values()` は **`update_client_toml` 互換のドット section 形＋型付き**（should#8）。
  3. **型対応 settings_store**（`settings_store.py`）: bool/int/float 対応（`isinstance(bool)` を先に判定）＋既存の演算子優先順位バグ修正（should#7）。
  4. **計測の可視化**（`ws_client.py`+`main_window.py`）: `on_state("metrics", dict)`（kind/ttfa_ms/stt_ms/codec、未観測は None、stream 開始でクリア）＋ステータス行表示（should#11）。
  5. **エラー/状態 UI の一貫化**＋**出力 combo 初期選択反映**（`main_window.py`+`app.py`）: `_on_state` を UI 反映の単一窓口に。`cfg.output.device` を combo 初期選択へ（should#9）。
- スコープ外: AEC/バージイン（pause で代替）・デバイスホットスワップ・マルチプロファイルUI・i18n・テーマ・Opus アップリンク。

## 2. 既存資産
- `client/audio/mic.py`: `MicCapture`（worker ブロッキング read→`segmenter.feed`→`on_segment`、`start`/`stop`、`call_soon_threadsafe`、`flush` で末尾セグメント emit）。**pause/resume を追加**。
- `client/ws_client.py`（X2a後）: `on_state` イベント（ready/connected/busy/idle/disconnected/reconnecting/error/transcript）、`_t_send`、TTFA ログ（`_on_binary` 初音）、`transcript` 受信（C2）。`is_connected`/`reconnect`/`_apply_server_settings`?（未実装＝本書で追加）。**計測 state を追加**。
- `client/ui/main_window.py`: 出力 combo（`selected_output_device`/`set_output_device` 無し）、接続ラベル（`set_connection`）、🎤トグル、ログ、歯車（`set_on_settings`）、`set_busy`、`set_mic_active`/`set_mic_enabled`。**ステータス行・`set_output_device` を追加**。
- `client/ui/settings_dialog.py`: 最小版（address/token/profile・フラット `values()`）。**全項目＋ドット section 型付き `values()` へ**。
- `client/settings_store.py`: `update_client_toml(path, dict[str,dict[str,str]])`（`_toml_str` で全部 quote・`[section]` 完全一致は `[mic.vad]` も group(1)="mic.vad" で拾える・**演算子優先順位バグ** `if found and stripped_j.startswith("#") or stripped_j == ""`）。
- `client/audio/devices.py`: `list_input_devices()`/`list_output_devices()`/`resolve_device(name|idx|'default', kind)`/`preferred_default_output()`。
- `client/config.py`: server(address/token/codec/tls_insecure/tls_cafile/token_in_query/reconnect/reconnect_max_delay)・output(device/resample)・mic(device/sample_rate/channels/preprocess(gate/denoise/gate_threshold)/vad(engine/min_silence_ms/speech_pad_ms/aggressiveness/frame_ms/min_speech_ms/max_speech_ms))・profile(active_id)。
- `client/app.py`: `_on_state`・`on_mic_toggle`/`on_segment`（busy-skip ログ ≤1/s）・`on_settings`（最小保存→reconnect）・`mic_holder`・`_shutdown`。

## 3. 設計

### 3.1 マイク pause/resume（`mic.py`・critical#5/should#6）
- `MicCapture` に追加: `self._paused = threading.Event()`（**set=pause**）、`self._reset_pending = threading.Event()`、`self._discard_tail = False`。
  - `pause()`: `if not self._paused.is_set(): self._paused.set(); self._reset_pending.set(); self._discard_tail = True`（冪等）。
  - `resume()`: `self._paused.clear()`（reset 不要＝idle から再開）。
  - `@property paused`: `self._paused.is_set()`。
- worker `_run`: ループ内で **read は継続**（デバイス overflow 防止）が、
  ```
  if self._paused.is_set():
      if self._reset_pending.is_set():
          self._segmenter.reset(); self._reset_pending.clear()
      continue                      # フレームを segmenter に流さず破棄
  for seg in self._segmenter.feed(pcm): self._emit_segment(seg)
  ```
  - 停止時の `flush()`: **`if not self._discard_tail: tail = self._segmenter.flush()...`**。pause 中(=discard_tail)に stop された場合は flush 尾を emit しない（should#6）。resume すると `_discard_tail=False` に戻す（resume で再び発話を拾うため）。
- **スレッド安全**: `_paused`/`_reset_pending` は Event（main→worker 一方向）。`segmenter` は worker 専有（reset も worker 内）。read は pause 中も回しデバイスを溜めない。
- **app 配線**（`_on_state`・critical#5）: 競合を避けるためガードを明示。
  - `"busy"` → `if mic_holder[0] is not None and mic_holder[0].active and not shutting_down: mic_holder[0].pause()`。
  - `"idle"` → `if mic_enabled_by_user and mic_holder[0] is not None and mic_holder[0].active and not shutting_down: mic_holder[0].resume()`。
  - `mic_enabled_by_user`（トグル状態）・`shutting_down`（終了/設定変更中フラグ）を app に持つ。設定変更で MicCapture を破棄する前に `shutting_down`(or `mic_reconfiguring`) を立て pause/resume 経路を無効化→破棄後にクリア。
  - C2 の `on_segment` busy-skip（≤1/s ログ）は**保険として残す**（pause 反映前に来た最後の1セグメント対策）。

### 3.2 型対応 settings_store（`settings_store.py`・should#7）
- **`_toml_value(v)`** を新設（`_toml_str` を内包）:
  ```python
  def _toml_value(v) -> str:
      if isinstance(v, bool): return "true" if v else "false"   # bool を int より先に！
      if isinstance(v, int): return str(v)
      if isinstance(v, float): return repr(v)
      return _toml_str(str(v))                                   # 既存 quote＋エスケープ
  ```
- `update_client_toml` の値書き出しを `_toml_str(...)` → **`_toml_value(...)`** に差し替え（既存キー置換・欠損追記の両方）。
- **演算子優先順位バグ修正**（should#7）: `if found and stripped_j.startswith("#") or stripped_j == "":` → **`if found and (stripped_j.startswith("#") or stripped_j == ""):`**。
- ネスト section（`[mic.vad]`/`[mic.preprocess]`）は section 名 `"mic.vad"` 等で updates に渡せば既存ロジックで書ける（`_SECTION_RE` が group(1)="mic.vad" を拾う）。**新規 section 追記時の順序は末尾**で可。
- 後方互換: str 値は従来通り quote。既存の C1/X1/X2a の scalar-str 呼び出しは不変。原子的書き込み・tomllib 検証・コメント保持は維持。

### 3.3 全設定 GUI（`settings_dialog.py`・should#8）
- `SettingsDialog(parent, cfg, profiles, input_devices, output_devices)`。`QVBoxLayout` に **QGroupBox×3**:
  - **サーバ**: address(`QLineEdit`)・token(`QLineEdit` Password)・codec(`QComboBox`: opus/pcm)・tls_insecure(`QCheckBox`)・tls_cafile(`QLineEdit`)・profile(`QComboBox`: 「(サーバ既定)」＋profiles)。
  - **マイク**: device(`QComboBox`: 「既定」＝""＋ `list_input_devices()` を name/index)・aggressiveness(`QSpinBox` 0-3)・min_silence_ms(`QSpinBox` 100-3000)・speech_pad_ms(`QSpinBox` 0-1000)・gate(`QCheckBox`)・gate_threshold(`QSpinBox` 0-5000)。
  - **出力**: device(`QComboBox`: 「既定」＝"default"＋「TTS_Virtual」＝"__ttsvirt__"＋ `list_output_devices()`)。
  - 各ウィジェットは cfg の現在値で初期化。device combo は現在値（name 一致/index/"default"）を選択。
- **`values() -> dict`（ドット section 型付き・should#8）**:
  ```python
  {
    "server": {"address": str, "token": str, "codec": str, "tls_insecure": bool, "tls_cafile": str},
    "profile": {"active_id": str},
    "mic": {"device": str},
    "mic.vad": {"aggressiveness": int, "min_silence_ms": int, "speech_pad_ms": int},
    "mic.preprocess": {"gate": bool, "gate_threshold": int},
    "output": {"device": str},
  }
  ```
  - app はこれを **そのまま `update_client_toml(config_path, values)`** に渡せる（変換不要）。
- デバイス列挙失敗（portaudio 不在）は空リスト→「既定」のみ（C1 流儀・握りつぶし）。

### 3.4 計測の可視化（`ws_client.py`+`main_window.py`・should#11）
- **WSClient**: `on_state("metrics", dict)` を発火。payload:
  ```python
  {"kind": "speak"|"utter", "ttfa_ms": float|None, "stt_ms": float|None, "codec": <ready.codec|None>}
  ```
  - `speak()`/`utter()` 開始時（busy 直後）に **`_state("metrics", {"kind":..., "ttfa_ms":None, "stt_ms":None, "codec":...})`** を出して**前回値をクリア**（should#11: 古い値を残さない）。`_metric_kind` を保持。
  - `transcript` 受信時（utter 経路）: `stt_ms = (loop.time()-_t_send)*1000` を計算し `_state("metrics", {"kind":"utter","stt_ms":stt_ms,"ttfa_ms":None,"codec":...})`。
  - 初音（`_on_binary` の first chunk）: 既存 TTFA ログに加え `_state("metrics", {"kind":_metric_kind,"ttfa_ms":ttfa_ms,"stt_ms":<前回stt or None>,"codec":...})`。stt と ttfa を同 payload に持たせるため `_last_stt_ms` を保持して同梱してもよい（実装簡潔さ優先で可）。
- **MainWindow**: `set_status(text)` で**ステータス行**（`statusBar()` か下部 `QLabel`）に「codec:opus | STT 1.20s | TTFA 130ms」等を表示。接続状態は既存 `set_connection`（または status に統合）。
- ログ（log_view）は従来の詳細用に残す。metrics はステータス行の要約。

### 3.5 エラー/状態 UI の一貫化＋出力 combo 反映（`main_window.py`+`app.py`・should#9/nit#15）
- **`_on_state` を UI 反映の単一窓口**に（X2a の payload 表に準拠）:
  - `ready`→`set_connection("✓ codec:"+payload.codec)`＋mic_enabled（auto_tts）、`connected`→log、`busy`→`set_busy(True)`＋mic.pause、`idle`→`set_busy(False)`＋mic.resume、`disconnected`→`set_connection("✗")`＋`set_busy(False)`、`reconnecting`→`set_connection(f"再接続中…({payload})")`、`error`→`set_status(f"⚠ {payload}")`＋log、`transcript`→log（C2）、`metrics`→`set_status(...)`。
- **出力 device 初期選択反映（should#9）**: `MainWindow.set_output_device(value)` を追加し、起動時に `cfg.output.device` を combo の初期選択へ反映（name 一致/index/"default"/"__ttsvirt__" を明示処理）。設定保存で output.device が変わったら `set_output_device(new)`。`selected_output_device()` は既存（device_provider が参照）。

### 3.6 設定の即時反映（`app.py on_settings`）
- ダイアログ OK で:
  - `update_client_toml(config_path, vals)`（vals は §3.3 のドット section 型付き dict をそのまま）。
  - `cfg` をメモリ更新（server/mic/output/profile の各フィールド）。
  - **サーバ系変更**（address/token/codec/tls_insecure/tls_cafile）→ WSClient へ反映する **`ws._apply_server_settings(codec_pref=, tls_insecure=, tls_cafile=, token=)`**（新規 setter）＋ `await ws.reconnect(address, token)`。
  - **マイク系変更**（device/vad/preprocess）→ `mic_reconfiguring=True`（pause/resume 経路無効化）→ 既存 MicCapture を `stop()`→`mic_holder[0]=None`→`mic_reconfiguring=False`。次回トグル ON で新 cfg から再生成（または ON のままなら即再生成して start）。
  - **出力 device** → `window.set_output_device(cfg.output.device)`。
- **`ws._apply_server_settings(...)`（新規）**: `self._codec_pref`/`_tls_insecure`/`_tls_cafile`/`_token` を差し替えるだけの setter（reconnect は呼び出し側）。

## 4. 受け入れ基準
- [ ] **マイク pause/resume ユニット**（fake stream/segmenter 注入）: `pause()` 後は `on_segment` 発火せず・pause 遷移で `segmenter.reset()` が1回・`resume()` で再開・冪等。**pause 中に stop すると flush 尾を emit しない**（should#6）。
- [ ] **settings_store 型対応ユニット**: bool→`true`/`false`（`True`→`"1"` でない）・int→数値リテラル・float→数値・str→quote、を書いて tomllib 再読込で型一致。`[mic.vad]`/`[mic.preprocess]` 既存キー置換・欠損追記・コメント保持・原子的書き込み。演算子優先順位修正で found でない空行に反応しない。
- [ ] **全設定ダイアログユニット**（`QT_QPA_PLATFORM=offscreen`）: cfg から各ウィジェット初期値・`values()` が §3.3 のドット section 型付き dict（bool は bool/int は int）・device 列挙失敗でも「既定」。
- [ ] **計測ユニット**（fake WS）: `utter`→`transcript`→初音 で `on_state("metrics", ...)` に `stt_ms`/`ttfa_ms`/`codec`/`kind` が乗り、stream 開始で前回値クリア（None）。`speak` 経路は `kind:"speak"`・stt_ms は None。
- [ ] **出力 combo 反映ユニット**: `set_output_device("pulse")`/index/"default"/"__ttsvirt__" で正しい combo 選択。
- [ ] **後方互換/境界/コンパイル**: 既存全テスト green（C1/C2/S2/X1/X2a）。`update_client_toml` の既存 str-only 呼び出し不変。AST 境界・py_compile。**Qt テストは `tests/conftest.py` の offscreen＋session `qapp` fixture**（nit#12 相当・X2b で導入）。
- [ ] **統合（手動・私がLinuxで）**: GUI 起動→🎤ON で発話→**再生中はマイクが止まり**(ステータス/挙動で確認)・idle で再開（フィードバック無し）。設定ダイアログで mic device/VAD/codec/出力 を変更→保存→反映（サーバ系は再接続）。ステータス行に codec/STT/TTFA 表示。サーバ再起動で再接続（X2a）。
- [ ] **Windows実機（あなた）**: マイク pause・全設定UI・ステータス表示・出力切替。

## 5. 変更/新規ファイル
- `client/audio/mic.py`（`pause`/`resume`/`paused`・worker の破棄＋reset・flush 尾抑制）
- `client/settings_store.py`（`_toml_value` 型対応・演算子優先順位修正）
- `client/ui/settings_dialog.py`（全項目 QGroupBox・ドット section 型付き `values()`・device 列挙）
- `client/ui/main_window.py`（`set_status` ステータス行・`set_output_device`・`_on_state` 反映先の追加）
- `client/ws_client.py`（`on_state("metrics")`・`_apply_server_settings`・計測 state）
- `client/app.py`（mic pause/resume 配線＋ガード・全設定保存＆即時反映・metrics→status・出力反映・`mic_enabled_by_user`/`shutting_down`/`mic_reconfiguring`）
- `tests/conftest.py`（**新規**: `QT_QPA_PLATFORM=offscreen`＋session `qapp` fixture）
- `tests/test_mic_pause.py`（**新規**）・`tests/test_settings_store.py`（型対応追加）・`tests/test_settings_dialog.py`（**新規**）・`tests/test_ws_client.py`（metrics 追加）・`tests/test_main_window.py`（**新規** or 既存に output 反映）

## 6. テスト/検証
- **ユニット（GPT-SoVITS/実マイク不要）**: §4。Qt テストは offscreen＋共有 `qapp`、widget 初期値/values 検証のみ（イベントループは回さない）。fake stream/segmenter・fake WS・一時 toml。
- **統合（手動）**: §4。Linux は私、Windows はあなた。
- AST境界・py_compile・全 pytest green。**新規依存ゼロ**。

## 7. リスク・留意点
- **busy/idle 経路の多様性（critical#5）**: `idle` は cancel/切断/error/空transcript/TTS終了 の全経路で来る。resume は `mic_enabled_by_user and mic_holder is not None and mic.active and not shutting_down/mic_reconfiguring` を AND。設定変更での MicCapture 破棄前に pause/resume を無効化するフラグを立てる。二重 pause/resume は Event 冪等で安全。
- **pause 後の flush 尾（should#6）**: pause 中に stop すると `_discard_tail` で flush 尾を捨てる。resume で `_discard_tail=False`。
- **settings_store bool/int 順（should#7）**: `isinstance(v, bool)` を `isinstance(v, int)` より**先に**（bool は int サブクラス）。演算子優先順位 `(... or ...)` を括弧で。
- **`values()` 契約（should#8）**: ダイアログは最初から `update_client_toml` 互換のドット section 型付き dict を返す。app の変換を無くす。
- **計測の経路差（should#11）**: speak には transcript 無し（stt_ms=None）、`utter(auto_tts=False)` は初音無し（ttfa_ms=None）。stream 開始で前回値を None クリアし古い値を残さない。`kind` を payload に。
- **Qt テスト（nit#12）**: `tests/conftest.py` に offscreen＋session `qapp`。各テストで QApplication を作らない。
- **設定即時反映**: サーバ系=`_apply_server_settings`＋reconnect(X2a)、マイク系=MicCapture 再生成、出力=combo。device ホットスワップは対象外。
- **後方互換が最重要**: 既存全テスト green。`update_client_toml` の str-only 既存呼び出し不変。`_on_state` 既存分岐（ready/connected/busy/idle/disconnected/reconnecting/error/transcript）を壊さず metrics/status を足す。MicCapture の既存 start/stop/on_segment 不変。
- **新規依存ゼロ**。
