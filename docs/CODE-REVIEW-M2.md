# コードレビュー: M2 実装（GLM-5.1 委託分）

レビュアー: Claude（統合担当）。対象: GLMが `docs/IMPL-M2.md v2` に基づき実装した6+1ファイル。

## 総合判定: 合格（1件の要修正バグあり・C1で対応）
仕様 `IMPL-M2.md v2` に忠実。検証も通過：
- `py_compile` 全7モジュール OK
- ユニットテスト `tests/test_pcm.py` **15/15 passed**
- 実サーバ(:9880) `client.stream` **first-chunk = 148ms**（目標≤400ms）
- `synthesize` 回帰 OK（M1経路維持）
- `StreamingPlayer` 無音スモーク（奇数長chunkでcarry誘発）例外なし

## 良い点
- `_build_request_kwargs` 一本化で `synthesize`/`stream` が同一ロジック（codex指摘#1反映）。
- `stream` は `async with self._client.stream(...)` で **cancel安全**（codex#2反映）。
- `_pcm_to_float32` は未知encodingで `ValueError`（暗黙fallback無し）、24bitは非対応で安全（codex#4反映）。
- `StreamingPlayer`: carry境界処理、preroll、tail-drain（sleep(latency)後close）、idempotentなclose/abort、write例外でabort+raise（codex#3,#7反映）。
- `routing.move_app_to_sink`: pactl存在/sink存在/候補(pid+app名)選択/最新id/timeout付subprocess/全失敗ログ＆None（codex#8,#9反映）。
- UI: 停止/`set_busy`(同時発話防止)/TTS_Virtual項目/戻り型 `int|None|str`（codex#10反映）。

## 要修正（1件）
### [高] ストリーミング再生で発話の先頭が欠ける
- 症状（ユーザー実機）: 最初が途切れて読まれない。
- 原因: `StreamingPlayer.__init__` で `OutputStream.start()` を即実行するため、最初のチャンク到達(~148ms)まで**枯渇状態で空回り**し、頭が飲まれる。preroll(40ms)は `start()` 後の蓄積で、空回り自体は防げていない。
- 修正方針（**C1で実装**・再生はクライアント側に残るため有効）:
  - **遅延スタート**: `__init__` では `OutputStream` を生成のみ（`start()` しない）。最初に preroll が満たされて書き込む直前に `start()`。
  - preroll を ~80–150ms に増やしジッタ吸収（TTFAは +preroll するが LAN想定で目標内）。
  - `close`/`abort` は未スタートでも安全に（既存の `_closed` ガードに加え未start時の stop/close をガード）。

## 軽微
- `app.py` の `PULSE_PROP` 設定行が**プレースホルダ無しの f-string**（ruff F541相当）。`f"..."` を通常文字列に。
- `routing.py` の `move-sink-input` は `returncode` 未チェック（失敗でも成功ログ）。best-effortだが `result.returncode` を見てログを分けると堅牢。
- `tests/` のため pytest を dev依存グループ(PEP735)に追加済（妥当）。

## 注記（v0.3 への影響）
- 本M2コードは **クライアント–サーバ分離(v0.3)** で配置換えされる：`StreamingPlayer`/`devices`/`ui`→クライアント、`tts client/profile`→サーバ。`routing.py`(PipeWire)はLinux/dev用、Windows本番はデバイス選択。
- 本コミットは **分離リファクタ前のM2チェックポイント**として保存する。
