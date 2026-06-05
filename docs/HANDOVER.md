# HANDOVER — 引き継ぎ & ハマりポイント集

このプロジェクト(Text-to-shaberi)で**実際にハマった罠**を、次に触る人（未来の自分・他の開発者・AIエージェント）が二度落ちないよう記録する生きたドキュメント。

> **運用ルール（重要）**：新しくハマったら、その場で
> 1. このファイルに `罠 / なぜ / 回避` の形で追記する、かつ
> 2. GaOTTT に `remember`（日本語・`emotion` 付き＝失敗は負、成功は正で recall に浮きやすく）。
>
> 「コードを読めば分かること」（構造・過去の修正・git履歴）は書かない。**非自明な落とし穴・暗黙の前提・誤診しやすい症状**だけを書く。

---

## 1. テスト & デバッグ

- **async テストの「ハング」は uv ロックを疑う前に自作のテストバグを疑う。**
  実例：`notify(done)` の宛先を `_active_conn → _audio_conn` に変えたのに、ある既存テストが `mark_active()` を呼ばず `_audio_conn=None` のまま → done がキューに入らず `await ws.recv()` が**永久待ち**。pytest が20分回りっぱなしになった。→ **設計変更でその前提を変えるなら、依存する既存テストも必ず同時に更新する。**
- **async ハングの切り分け手順**：
  1. 該当 PID を**個別に** `kill -9 <pid>`（後述の `pkill -f` 厳禁）。
  2. テストを**1ファイルずつ** OS タイムアウト付きで回す：`timeout 60 uv run pytest -q -p no:cacheprovider tests/<file>.py; echo rc=$?`（`rc=124` がタイムアウト）。どのファイルで詰まるか二分する。
  3. `pytest -v -s` でも**出力はバッファ**され、Terminated 時に犯人名が出ないことがある。最終手段は個別テスト名で `timeout` 実行、または `python - <<'PY' ... PY` の inline 再現スクリプト。
- **`pkill -f <pattern>` は厳禁**：実行中の自分のシェル/エージェントプロセスもパターンに一致して**自滅**する。必ず `ps -eo pid,args | grep ... | awk '{print $1}' | xargs -r kill -9` で**PID指定**。
- **複数の `uv run` を背景並走させない**：本当の uv ロック競合（パッケージ解決の file lock）で見かけ上ハングする。検証は直列で。
- **pytest は一時ディレクトリが要る**：read-only/temp 不可の環境（codex の sandbox 等）では `FileNotFoundError: No usable temporary directory found` で collection 前に落ちる。codex にテスト実行を期待せず、静的レビュー＋こちらでの pytest 実行を分担。
- **実起動エンドツーエンド smoke が効く**：component test では app.py の**配線(seam)**が検証できない。`websockets.serve` で ~40行の**偽サーバ**（hello→ready→stream_begin→audio返却）を立て、`QT_QPA_PLATFORM=offscreen` で GUI をそこに繋ぎ、入力口へ実 WS 投入して `accepted→audio_begin→binary→audio_end→done` を実測する。
- **GUI専用バグは pytest をすり抜ける**（qasync の `aboutToQuit` 等）。offscreen 起動 smoke で AttributeError/NameError/TypeError を grep。
- **敵対テストの作り方**：`asyncio.Event` で `await` をブロックできる FakeWS（`start_block`/`end_block`/`speak_block`/`send_block`/`*_result`）を用意し、「await 中に cancel/idle/例外を割り込ませる」レースを**決定的に**再現する。

## 2. 非同期 / 並行制御（forward_audio で9ラウンド確立）

`入力→bridge→ws→サーバ→音声` のように**非同期パイプラインでリソースを発生元へ正しく結びつける**設計の定石。pytest 緑でも本物の競合が残るので codex レビュー必須（§4）。

- **check-then-set 競合**：通知側（`notify_ws_idle`）がフラグ（`_awaiting_done`）を**読む**なら、そのフラグは `await` の**前**に立てる。await 後に立てると、await 中に来た通知がフラグを見逃し **done ゼロ（永久 busy）**。逆に await 後に戻り値 False でフラグを戻すと、**既に通知が消費済みなら二重 done**。→ False 処理時は「フラグがまだ True か（=未消費か）」で分岐する。
- **終端イベント(done)の保証**：consumer を **per-event `try/except`** で包み、例外でも終端を必ず1回出す。例外/bail で状態を潰してよいかは **世代カウンタ(`_gen`)一致**で判断（abort が割り込んだら潰さない）。
- **キャンセル**：フラグを落とすだけでは漏れる。`abort()` で **pending queue を drain ＋ in-flight を ws.cancel ＋『リソースがもう出ない時点(=idle)』でだけ解放**。早期解放は新接続への漏れ窓を作る。
- **宛先 pin**：音声/完了通知の宛先は『その時点の最新 active 接続』ではなく『**request を起こした接続**』に固定し、**完了まで新接続を busy 拒否**する。これで late audio の漏れを構造的に排除。
- **bounded queue は制御イベントを落とさない**：満杯時に最古を無条件 drop すると `begin/end/done` が消える。**chunk(データ)だけ drop**し、制御は超過許容で必ず入れる（`deque` で chunk を選んで退避）。
- **判断軸**：reservation+abort のような状態機械で**角(エッジ)が3ラウンド以上続く**なら、潰し続ける前に**設計簡素化**（状態数を減らす再設計）を選択肢としてユーザーに出す。
- **broadcast tee（音声モニタ）の落とし穴**：全購読者へ配る tee は loop スレッド**同期・await 無し**で `list(subs)` を回し iteration 競合を避ける。bounded queue は forward_audio と同じく chunk のみ drop だが、**単一 pin 接続と違い「制御イベント(begin/end)を超過許可で必ず入れる」はNG**——死/停滞購読者で begin/end が無限蓄積し**購読者単位メモリリーク**になる。退避する chunk が無い（=全く drain していない）購読者は**切断**する（framing 保証 or 接続終了）。tee の callback 例外は**握って再生/recv ループへ漏らさない**（best-effort）。emit する側（ws_client）はキャンセル経路の単一終端 `_finish_stream` で end を1回。
- **スピーカーミュートの落とし穴**：ミュートを「`feed()` を止める」で実装すると**不十分**——engine/sink の非同期キューや `StreamingPlayer` の preroll に**投入済みの非ゼロ音声が鳴り続ける**（mid-stream/gapless で顕著）。正解は**再生 worker のデバイス write 直前でフレームを無音化**（`engine._write_chunked` / `StreamingPlayer._write`＝prime 以外の全 device write を集約）。キューは実時間で消費＝即時無音、**seq 不変なので unmute で即再開**（TVのミュート）。`feed`/`begin`/`end` は通常どおりで monitor/forward_audio/STT には波及しない。`PlaybackSink`→`StreamingPlayer` の `set_muted` 伝播は、player を **worker スレッドで生成**するため lock で『公開＋初期反映』と main の `set_muted` を**直列化**（lost update 防止）。ミュートは**選択中の出力デバイス（仮想シンク含む）**を消すので OBS 配信時は使わない。codex round-1〜4 で feed-gate→engine→preroll→lost-update の順に潰した。

## 3. GLM 委託（外部LLMへの実装委託 / `mcp__secondopinion__delegate_task`）

- **大きな委託は推論トークン上限で 0 出力**（`finish_reason:"length"`, output≈9トークン, 編集ゼロ）。→ **1ファイル=1委託の小タスク**に割り、「extended reasoning なしで今すぐ1ファイル編集」＋自己検証コマンドを必ず付ける。**完全一致の old→new ブロック**を渡すと apply-patch 同然で信頼性が高い。
- **GLM 並走で uv ロック/VRAM 競合** → 誤ハング・誤 OOM に見える。委託は直列で。
- **起動即 stall は再委託**で回復することが多い。`mtime` が ~120s 静止したら別タスクへ。
- GLM は省略・抜けをする（初期の `set_mic_enabled(False)` 等）。**委託後は必ず diff を自分で確認**し、抜けは手で埋める。

## 4. codex レビュー（`codex exec`）

- **必ず `< /dev/null`**：stdin を閉じないと対話待ちでハングする。`codex exec -s read-only "<prompt>" < /dev/null`。
- **自作コードも必ずレビューさせる**（May の方針）：pytest 緑＋smoke OK でも、並行/キャンセル/寿命/順序の本物バグは正常系テストをすり抜ける。**GO が出るまで**回す。各ラウンドで「全タイミングで done がちょうど1回か」のような**不変条件を明示的に問う**。
- codex は自分の sandbox で **inline 再現スクリプトを実行**して指摘してくることがある（信頼度が高い）。指摘は**敵対テスト化**して回帰に残す。

## 5. Docker / サーバ運用

- **クライアントが「サーバ対応機能」をグレーアウト＝サーバの Docker イメージが古い**。`ready.features` に新機能が無いと client が UI を抑止する。→ `docker compose build orchestrator && docker compose up -d orchestrator` で作り直す。**機能追加後はイメージ再ビルドを忘れない**。
- **`GPTSOVITS_DIR` はマシン毎に必須**。プレビルドイメージは GPT-SoVITS の*コードのみ*でリポジトリの重みは持たない。`.env` の `GPTSOVITS_DIR` が実在しないと Docker が空ディレクトリをマウントして `api_v2.py` が隠れ、`gpt-sovits` が再起動ループ→orchestrator も起動失敗。起動前に `ls "$GPTSOVITS_DIR/api_v2.py"` と `ls "$SHABERI_ASSETS/ref.wav"`。
- compose は native 版の**置き換え**（同じ 8765/9880/GPU）。native を起動したまま `up` するとポート衝突。

## 6. セキュリティ / コミット

- **`deploy/server.docker.toml` は token 入りで gitignore。絶対にコミットしない。** `.env` / `assets/*.wav`(ref.wav 含む) も gitignore。
- **`ToDo.md` は作業者の私物**。ステージは `git add README.md docs/ src/ tests/` のように**対象を明示**し、`git status --short` で `ToDo.md` が `??` のままか確認。
- 進め方：feature ブランチ→commit→`git merge --ff-only` main。**push は明示指示があるまでしない**。コミット文末は `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

## 7. アーキテクチャ上の不変条件

- **VAD/音声分割は必ずクライアント側**（無負荷・将来スマホ化想定）。サーバは STT/TTS のみ。
- speak_stream / text-stream / forward_audio は**同じ `active_stream_session` スロットを共有** → mic と LLM入力は**同時併用不可**（busy で弾く仕様）。
- 音声ワイヤ形式：**Opus は 1 バイナリWSメッセージ = 1 パケット**（48kHz mono / 20ms / 960サンプル、長さ前置きなし、連結デコード禁止）。PCM はメッセージ境界を無視して連結（s16le）。詳細は [`PROTOCOL.md`](PROTOCOL.md)。
- クライアントのローカル口は**2つ**：入力口 `[input]`(8770, テキスト投入・forward_audio で同接続に音声返却・**単一接続+reservation**)と、音声モニタ `[monitor]`(8771, 全合成音声の **broadcast**・受信専用・複数接続・reservation 無し)。役割と並行ロジックが別物なので**混ぜない**。

---

*最終更新の起点：2026-06-05（forward_audio 実装、codex 9ラウンドの知見を集約）。以後ハマる度に追記。*
