# R0 実装計画書 v2: リポジトリ再編（クライアント–サーバ分離の土台）

> 上位設計: `docs/ARCH-v0.3-client-server.md`(rev2)。フロー: 本書 → GLM委託 → 検証。
> **v2: codexレビュー(`docs/REVIEW-codex-R0.md`)反映** = client `--check`/MainWindowをClientConfigのみで成立／既定パスparents[3]／`__main__.py`存続／AST静的import境界テスト／R0で client発話はstub化と明文化／bind検証ipaddress／protocol型補強／profile全要素移設。
> 対象: `/mnt/holyland/Project/Text-to-shaberi`（Python3.12 / uv）。**この仕様の通りに実装すること。**

## 1. 目的・スコープ
既存モノリシック実装(M0/M1/M2/IME)を **common / server / client** に再編し、依存をグループ分離する。
- **スコープ内**: ファイル移設、`common`抽出、依存グループ、2エントリ、import境界＋AST検証、client-only起動検証、テスト更新、config分割。
- **スコープ外（後続）**: WS本実装(S1/C1)、STT(S2)、lazy-start修正(C1)。
- **動作方針（重要・矛盾しないよう明文化）**: 低レベルロジック（PCM変換/プロファイル展開/StreamingPlayer等）は**不変で移設**。ただし **client の発話(speak)は意図的に stub 化**し、**既存の「テキスト→TTS→再生」動作は S1/C1 まで一時的に失われる**（client が server/TTS を直叩きしない分離のため）。

## 2. ターゲット構成
```
src/text_to_shaberi/
├── __init__.py                 # 最小化（再エクスポートしない。version等のみ）
├── __main__.py                 # 存続。中身を `from .client.app import main` に変更
├── common/
│   ├── __init__.py
│   ├── pcm.py                  # _pcm_to_float32 / bytes_per_sample / split_full_samples / AudioBuffer
│   ├── audio_format.py         # AudioFormat / CodecFormat
│   ├── protocol.py             # pydantic WSメッセージスキーマ（§3.3）
│   └── config.py               # 共通ローダ helper（_only / tomllibロード）
├── server/
│   ├── __init__.py
│   ├── config.py               # ServerConfig（server.toml）
│   ├── app.py                  # run_server（R0 stub: 設定ロード＋bind検証＋uvicorn起動）
│   ├── ws_server.py            # create_app()（/healthz＋/ws stub。S1で本実装）
│   ├── tts/{__init__,client,profile}.py   # 旧 tts/ 移設（PCM/AudioFormatはcommon参照）
│   └── stt/__init__.py         # S2用プレースホルダ
├── client/
│   ├── __init__.py
│   ├── config.py               # ClientConfig（client.toml）
│   ├── app.py                  # run_gui＋IME＋--check（旧 app.py 移設・speakはstub）
│   ├── ws_client.py            # stub（C1で実装）
│   ├── audio/{__init__,devices,player,routing}.py  # 旧 audio/ 移設（player は common.pcm参照）
│   └── ui/{__init__,main_window}.py                # 旧 ui/ 移設
└── （旧 tts/ audio/ ui/ api/ config.py app.py 削除、config.toml 削除）
```

## 3. 詳細

### 3.1 common/pcm.py
旧 `tts/client.py` から **`_pcm_to_float32`・`bytes_per_sample`・`split_full_samples`・`AudioBuffer`** を移設（ロジック不変）。理由: PCM decode の戻り値型として server TTS と将来の client decode/再生テストで共有し、**client→server 依存を断つ**（codex#1,#12）。`numpy` 依存は common で許容。

### 3.2 common/audio_format.py
- `AudioFormat`(encoding, sample_rate, channels) 移設。
- `CodecFormat`(codec="pcm_s16le", codec_rate:int|None=None, frame_ms:int|None=None) 新設（X1 Opus用の器。R0はPCM既定のみ）。

### 3.3 common/protocol.py（pydantic v2・型定義のみ）
ARCH §4 のメッセージを定義。`type` は **`Literal[...]`**。共通ペイロード型も用意：
- `AudioFormatPayload{codec:str="pcm_s16le", sample_rate:int, channels:int}`、`ProfileInfo{id:str, name:str}`
- `Hello{type:Literal["hello"], protocol_version:int=1, client_name:str, client_id:str, caps:dict}`
- `Ready{type:Literal["ready"], session_id:str, server_time:float|None=None, codec:str, tts:AudioFormatPayload, profiles:list[ProfileInfo], limits:dict, features:dict}`
- `Speak{type:Literal["speak"], id:str, text:str, profile:str|None=None}`
- `SttBegin{...,"stt_begin", id, format:AudioFormatPayload, auto_tts:bool=True}` / `SttEnd{...,"stt_end", id}`
- `Transcript{...,"transcript", id, text, final:bool=True}`
- `AudioBegin{...,"audio_begin", id, parent_id:str|None=None, format:AudioFormatPayload}` / `AudioEnd{...,"audio_end", id}`
- `Cancel{...,"cancel", id}` / `Stopped{...,"stopped", id}` / `ErrorMsg{...,"error", id:str|None=None, message:str}`
- `parse_message(data)->モデル`（`type`で判別）。**状態機械ロジックは持たない**（S1/C1）。

### 3.4 common/config.py
旧 `config.py` の `_only`（dataclassフィールド抽出）と tomllib ロードの薄い共通helperを移設。

### 3.5 server/config.py（ServerConfig）
`server.toml`：`[bind] host="127.0.0.1", port=8765, token=""` / `[stt] backend="qwen_local", language="ja"`＋`[stt.qwen_local]` / `[tts] profiles_dir, active_profile="gpt-sovits"` / `[transport] codec="pcm"`。
- **bind検証**（codex#10・`ipaddress`使用）: `host`をipaddressで解釈し **loopback(127.0.0.1/::1/localhost) 以外** へbindする場合は `token` 必須、空なら `ValueError`。解決不能なhostnameは安全側で token 必須。検証関数を用意（app起動時に呼ぶ）。

### 3.6 server/app.py・ws_server.py（R0 stub）
- `ws_server.py: create_app() -> FastAPI`（旧 `api/server.py` ベース）。`/healthz` を残し、`/ws` は stub（接続受理→`ready`返却→未実装ログ）。**`create_app()` は TestClient で叩ける形に**（§9）。
- `app.py: run_server()`：`ServerConfig`ロード→bind検証→uvicornで `create_app()` 起動。CLI `-c server.toml`。既定パスは `Path(__file__).resolve().parents[3] / "server.toml"`（src/text_to_shaberi/server/app.py から3段上＝repo直下）。

### 3.7 client/config.py（ClientConfig）
`client.toml`：`[server] address="ws://127.0.0.1:8765/ws", token=""` / `[output] device="default", resample="auto"` / `[mic] device, sample_rate=16000, channels=1`＋`[mic.preprocess]`＋`[mic.vad] engine="webrtc"` / `[ui] input_method="ibus"` / `[profile] active_id=""`。
- **VAD は設定値のみ**。`webrtcvad` の `import` は **C2 まで行わない**（R0で実処理に使わない・codex#13）。

### 3.8 client/app.py（run_gui＋IME＋--check 移設）★codex#1,#2,#3,#4
- 旧 `app.py` の `run_gui`/`_setup_input_method`/`_load_output_devices`/`run_check`/`main` を移設。importを新パスへ。
- **既定パス**: `DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "client.toml"`（client/app.py から3段上＝repo直下。codex#3）。
- **`--check` は ClientConfig のみ**で完結（**server/TTSプロファイルに触れない**・codex#1）: 表示は `server.address`/`token有無`/`output devices`/`IME(input_method)`/`active_profile_id`。**`load_profile` は import しない**。
- **`MainWindow` 初期ログを ClientConfig 由来に**（codex#2）: 旧 `config.active_tts_profile`/`config.stt.backend` は ClientConfig に無いので参照しない。代わりに `server.address` と `profile.active_id or "(server default)"` を表示。`MainWindow` が **ClientConfig だけで生成**できること。
- **speak は stub**: 送信時 `window.log("[client] WS未接続（C1で実装）")` のみ。`TTSClient` 直叩きは削除。停止/設定結線は C1。
- `client/ws_client.py`: `class WSClient` の空枠のみ（C1で実装）。
- `__main__.py`: `from .client.app import main` に変更（`python -m text_to_shaberi` 互換維持・codex#4）。

### 3.9 server/tts（移設・全要素）★codex#8
- `tts/client.py`→`server/tts/client.py`: PCMヘルパー/`AudioFormat` を **common参照**に変更。`TTSClient/_build_request_kwargs/stream/synthesize/_decode_raw/_decode_wav/_extract` は不変移設。
- `tts/profile.py`→`server/tts/profile.py`: **`render_value`・`TTSProfile`・`TTSProfile.render_body`・`load_profile`** を不変移設（`AudioFormat`はcommon参照）。`load_profile` の探索基準は ServerConfig の `profiles_dir`。
- `server/tts/__init__.py` の再エクスポート範囲を明記（TTSClient/TTSProfile/load_profile/render_value 等）。

### 3.10 client/audio（移設）
- `audio/player.py`→`client/audio/player.py`: PCMヘルパーを **common.pcm 参照**に。`StreamingPlayer/play/_device_samplerate/stop` 不変（**lazy-start修正はC1**）。
- `audio/devices.py`・`audio/routing.py`→`client/audio/`（不変、routingはLinux/dev保留）。

## 4. pyproject 依存グループ＋エントリ
- **トップレベル `dependencies` = common最小限**：`numpy`, `pydantic`。
- `[project.optional-dependencies]`:
  - `client = ["PySide6","qasync","sounddevice","soxr","numpy","websockets","webrtcvad"]`
  - `server = ["torch>=2.11.0","qwen-asr","fastapi","uvicorn","websockets","httpx","numpy","soxr"]`
  - 旧トップレベルの torch/qwen-asr/silero-vad/pyside6/sounddevice/... を各extraへ移動。`[[tool.uv.index]] cu128` 維持（torch用）。
- `[project.scripts]`: `text-to-shaberi-client = "text_to_shaberi.client.app:main"` / `text-to-shaberi-server = "text_to_shaberi.server.app:main"` / `text-to-shaberi = "text_to_shaberi.client.app:main"`(互換)。
- `[dependency-groups] dev=["pytest>=9.0.3"]` 維持。

## 5. 設定ファイル
旧 `config.toml` を **`client.toml`/`server.toml` に分割**（旧削除）。既存値を踏襲、VAD engine 既定は `webrtc`。`profiles/tts/gpt-sovits.toml` は据え置き（サーバ参照）。

## 6. import境界（codex#1,#2,#5）
- **`client/**` は `server/**`・`torch`・`qwen_asr`・`silero_vad` を import 禁止**（common と client 内のみ）。
- **`server/**` は `PySide6`・`sounddevice` を import 禁止**。
- `common/**` は torch/PySide6/sounddevice 非依存（numpy/pydantic/標準のみ）。
- 検証は **AST静的検査**を主、`sys.modules` を補助（§9）。

## 7. 受け入れ基準
- [ ] `py_compile` 全モジュール成功。
- [ ] `tests/` green（`test_pcm.py` import更新＋`test_import_boundaries.py` AST検査）。
- [ ] **クリーン環境で client-only**（codex#6）: `uv sync --extra client` 後に `uv run --extra client python -c "import importlib.util; assert importlib.util.find_spec('torch') is None and importlib.util.find_spec('qwen_asr') is None"` が成功。続けて `uv run text-to-shaberi-client --check` が **torch/qwen を import せず**完了。
- [ ] サーバ: `uv sync --extra server` 後、`server.ws_server.create_app()` を FastAPI TestClient で叩き `/healthz`=200（自動テスト）。bind検証＝外部bind+token空で `ValueError`。手動なら別プロセス起動＋`curl 127.0.0.1:8765/healthz`。
- [ ] client GUI 起動、送信で「WS未接続(C1)」ログ（クラッシュなし）。`python -m text_to_shaberi` でも起動。

## 8. 変更/新規/削除
- 新規: `common/{__init__,pcm,audio_format,protocol,config}.py`, `server/{__init__,config,app,ws_server}.py`, `server/tts/{__init__,client,profile}.py`, `server/stt/__init__.py`, `client/{__init__,config,app,ws_client}.py`, `client/audio/{__init__,devices,player,routing}.py`, `client/ui/{__init__,main_window}.py`, `server.toml`, `client.toml`, `tests/test_import_boundaries.py`
- 削除: `tts/`, `audio/`, `ui/`, `api/`, `config.py`, `app.py`, `config.toml`
- 変更: `pyproject.toml`, `__main__.py`(client.app:main へ), `tests/test_pcm.py`(import更新), `src/text_to_shaberi/__init__.py`(最小化)

## 9. テスト
- `tests/test_pcm.py`: import を `text_to_shaberi.common.pcm` に更新（内容不変）。
- `tests/test_import_boundaries.py`（新規・**AST静的検査**・codex#5）: `client/` 配下の全 `.py` を AST 解析し `Import`/`ImportFrom` に `text_to_shaberi.server` / `torch` / `qwen_asr` / `silero_vad` が無いことを assert。`server/` 配下に `PySide6` / `sounddevice` が無いこと。補助として `import text_to_shaberi.client.app` 後 `sys.modules` に torch/qwen_asr が無いことも確認。
- サーバ stub: `TestClient(create_app()).get("/healthz").status_code == 200` のテスト。
- 手動: §7 の client-only クリーン環境検証。

## 10. 留意点
- 移設は**ロジック不変**。挙動変更（lazy-start等）はR0で入れない（C1）。client speak の stub 化のみ意図的変更（§1）。
- 相対import段数（`server/tts/client.py`→`from ...common.pcm import`）。
- `__init__.py` で client→server を繋げない（再エクスポート禁止）。
- `webrtcvad`/`silero` は R0 で import しない（設定値のみ）。
