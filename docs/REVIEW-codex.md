# codex レビュー結果（PLAN.md v0.1 に対して）

- レビュアー: codex CLI 0.135.0 / model: gpt-5.5 / sandbox: read-only
- 対象: `docs/PLAN.md` v0.1, `README.md`

---

## 指摘一覧（重要度付き）

### 1. 低遅延要件の数値目標がない 〔高〕
READMEは「レイテンシ重視」が中核なのに、計画は最適化手段の列挙に留まり許容遅延が未定義。特にボイスチェンジャー用途は VAD終端待ち+STT+TTS でリアルタイムとしては厳しい前提。
**修正案**: `GUI/API入力のTTFA ≤ X ms`、`マイク往復 ≤ Y ms`、`発話終端待ち ≤ Z ms` の用途別SLOを定義。ボイスチェンジャー実用域を受け入れ基準に入れる前にPoC実測ゲートを置く。

### 2. VADセグメント単位STTでは文単位パイプラインの効果が限定的 〔高〕
文単位パイプラインはGUI/API入力には有効だが、マイク入力はSTTが発話セグメント確定後に始まるため、最初の音までの遅延短縮には効きにくい。
**修正案**: マイク経路の将来形 `音声チャンク→partial STT→文/句の暫定確定→TTS` を別ルートとして明記。MVPは「疑似ストリーミング」と正直に定義し、真の低遅延マイク用途は vLLM streaming/別ASR検証に分離。

### 3. GPT-SoVITS `streaming_mode` の前提が危うい 〔高〕
`streaming_mode = 3` 前提だが、`api_v2` はバージョン差があり bool 扱いの実装もある。整数固定だと最初のTTS接続で詰まる可能性。
**修正案**: 対象バージョンごとにプロファイルを分け、`streaming_mode` の型を `bool|int|string` 許容に。起動時に `/tts` 疎通テスト（Content-Type と最初のチャンク到達時間）をM1/M2に入れる。

### 4. toml汎用HTTPドライバの抽象が不足 〔高〕
JSON body + `{{text}}` 置換中心だが、実際のTTS差分は form-urlencoded / multipart / base64音声 / URL返却 / SSE/NDJSON / バイナリstream / 認証ヘッダ / ネストJSON path に広がる。
**修正案**: `request.body_type = json|form|multipart|raw_template`、`response.mode = audio_stream|audio_file|json_base64|json_url`、`response.extract.path`、`response.base64_format`、`request.files`、`template.vars` をschema化。テンプレは単純置換でなくJinja2相当か型を壊さない変数注入に。

### 5. キュー/割り込み/優先度の仕様が薄い 〔高〕
3入力を`TextQueue`に集約は妥当だが、順序・優先度・キャンセル・再生中断・TTS中HTTPキャンセル・チャンク再生停止が未定義。
**修正案**: `Utterance` に `id/source/profile/priority/interrupt_policy/created_at`。`enqueue|cancel|clear|preempt` を明示。割り込みは「再生停止だけ/TTSリクエストもキャンセル/キューも破棄」を分ける。

### 6. qasync+STTワーカースレッドの境界設計が未定義 〔中〕
方向性は妥当だが、sounddevice callback・asyncio・Qt signal・GPU推論スレッド間の受け渡し、シャットダウン、例外伝播が未設計。
**修正案**: `asyncio.Queue` と thread-safe queue の境界を明記。UI更新はQt signal経由限定。音声callback内は確保/ログ/await/重い処理を禁止しリングバッファコピーのみ。

### 7. Qwen3-ASRローカル統合は概ね正しいがVRAM「解決済み」は強すぎる 〔中〕
3090Ti同時常駐は、GPT-SoVITSのモデル構成・vLLMの`gpu_memory_utilization`・FlashAttention有無で変わる。
**修正案**: 「解決」でなく「検証ゲート」に。起動時VRAM計測、モデルロード順、OOM時フォールバックを設計。`language="Japanese"` と config `ja` のマッピングを実装前に固定。

### 8. 音声I/Oに sample rate 変換とデバイス再オープンが抜け 〔高〕
raw PCM 32kHz前提だが出力デバイスは48kHzが多く、TTSごとに 16k/24k/32k/44.1k/48k 混在。raw PCMはヘッダ無しで誤ると速度/音程が壊れる。
**修正案**: `AudioFormat(sample_rate, channels, sample_width, encoding)` をTTSレスポンス必須メタに。`samplerate`/`soxr` で出力rateへ変換。出力デバイス切替時は OutputStream を安全に drain/stop/reopen。

### 9. FastAPI内蔵サーバのライフサイクルとセキュリティが未定義 〔中〕
uvicornの起動/停止、ポート競合、WS切断、同時接続、外部公開時の認証が未定義。
**修正案**: 既定 `127.0.0.1`+token任意、外部公開時はtoken必須。uvicornは別スレッド起動かqasync統合かを決め、graceful shutdown を M0/M3 に。

### 10. ノイズ抑制がリアルタイム要件と噛み合っていない 〔中〕
`RNNoise/noisereduce` を同列に置いているが `noisereduce` 系は常時処理に不向きな場合あり。
**修正案**: MVPは VAD前段の軽量フィルタ+ゲイン/ノイズゲート程度に絞り、RNNoise等はオプション。ON時の追加遅延をメトリクス表示。

### 11. MVPフェーズ分割はSTTリスクの発見が遅い 〔高〕
STTがM5だが、最大リスクは「マイク→VAD→Qwen→TTS往復遅延」と「GPU同時常駐」。M5まで未検証だと手戻り大。
**修正案**: M1前後に `Spike-A: GPT-SoVITS raw streaming TTFA計測`、`Spike-B: Qwen3-ASR単発/疑似streaming遅延計測`、`Spike-C: 両モデル同時常駐VRAM計測` を追加。UI実装より先に性能PoC。

### 12. 受け入れ基準が機能確認寄りで性能・失敗時動作が不足 〔中〕
「鳴る/切替できる」中心で低遅延アプリの合否が曖昧。
**修正案**: `TTFA p50/p95`、`STT処理時間`、`TTS first chunk latency`、`再生underrun回数`、`割り込みから無音化までの時間` を受け入れ基準に追加。

---

## 総評
全体アーキテクチャの方向性は妥当（3入力→テキストキュー集約→tomlプロファイルでTTS差替→qasync UI→GPU推論はワーカースレッド）。ただし「低遅延で使えるか」を決める部分が未検証で楽観寄り。ボイスチェンジャー用途・GPT-SoVITS streaming raw・Qwen疑似streaming・VRAM同時常駐・tomlドライバ表現力は、実装前に仕様を締めるか小PoCで潰すべき。

## 実装着手前に必ず決めるべきTOP3
1. **用途別レイテンシ目標**（GUI/API TTFA、マイク往復、割り込み停止時間の合格ライン）
2. **TTSプロファイルschema**（JSON/form/multipart/raw、response抽出、base64/URL/stream、audio format を吸収）
3. **性能PoCの順序**（GPT-SoVITS raw streaming、Qwen疑似streaming、両モデル同時常駐をUI実装前に測る）
