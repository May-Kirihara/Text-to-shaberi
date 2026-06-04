# IMPL-SPEAKSTREAM — 喋りながら順次TTS（ライブマイク・増分STT→TTSパイプライン）

> 目的: マイクで**喋っている最中から**、句相当の単位で逐次 STT→TTS し、**順序付き・ギャップレス**に音声を返す。
> 入力ソース最優先 = **ライブのマイク発話**。実装 = **サーバ側 speak_stream（STT/TTSパイプライン）**。
> 制約 = **VAD/セグメント化はクライアント側**に残す（webrtcvad はほぼ無負荷・将来スマホ対応。[[smartphone-client-load-distribution]]）。

対象コミット基準: `0523af6`（docker compose 完走後）。

---

## 0. 設計の核心

「並列＋上書き」ではなく **2段パイプライン**:

```
クライアント(VAD)                サーバ speak_stream                       クライアント
─────────────              ─────────────────────────────           ─────────────
mic→VADで句単位に            seg を順に受信                            audio を到着順に
細かく確定(sub-seg)  ──音声──▶  ┌ STTステージ(先行可) ┐                   ギャップレス再生
                              │ seg N の STT 実行中に │                   (既存PlaybackEngine)
                              │ seg N+1 の音声も受信  │
                              └ TTS出力ステージ(順序直列) ┘ ──音声──▶
```

- **STTステージは先行**してよい（seg N の TTS を鳴らしている間に seg N+1 を STT）。
- **TTS出力ステージは厳密に index 順**（音声は並べ替えできないため、クライアントへ送る audio_begin/chunks/audio_end は seg 順に直列）。
- これで「並列の旨味が薄い1GPU」でも、**STTレイテンシを再生の裏に隠して**連続感を出せる。上書きは不要。

**なぜ現状のままでは不可:** サーバは `_busy()`（`orchestrator.py:83`）で**1セッション=1アクティブタスク**に直列化し、busy 中の `stt_begin`/`speak` を拒否（`:270,:376`）。STT も全バッファを `stt_end` で1回 transcribe（`:397,:420`）。= 1発話ずつブロッキング。これを streaming 経路では**パイプライン**に置き換える。

---

## 1. クライアント側（VAD ストリーミングモード）

VAD は据え置き（`client/audio/vad.py` の `VadSegmenter`）。新たに **streaming モード**を追加し、サブセグメント境界を細かくする。

- **サブセグメント確定条件**（どれか先に成立で1サブセグメント emit）:
  - 句相当の**短い無音** `seg_silence_ms`（例 250ms。既存の発話末 `min_silence_ms`=500 より短い）。
  - **最大長** `seg_max_ms`（例 2500ms。ポーズなしで喋り続けても切る）。
- **発話末**（既存 `min_silence_ms` 相当の長無音）で stream を**終端**（`stream_end`）。
- **最小長** `seg_min_ms`（例 400ms）未満の極小片は次へ連結（無音・ノイズの細切れ抑制）。
- 送出: 各サブセグメント音声を `seg`（index 付き）としてサーバへ。VAD/区切りはクライアント完結＝スマホでも同構図。

> 設計判断: 既存の「busy中はセグメントを捨てる」ロジック（`app.py` on_segment）は streaming では**捨てずにサーバのパイプラインへ流す**（サーバが順序保持）。

---

## 2. プロトコル拡張（`common/protocol.py`）

既存（`hello/ready/speak/stt_begin/stt_end/transcript/audio_begin/audio_end/cancel/stopped/error`）に、**ストリーミング発話セッション**を追加。

| メッセージ | 方向 | フィールド | 役割 |
|---|---|---|---|
| `stream_begin` | C→S | `id`, `format`(AudioFormat), `auto_tts=true` | ストリーム開始（=1発話の連なり） |
| `seg`（or seg_begin/seg_end+binary） | C→S | `id`(stream), `index` + 後続 binary | サブセグメント音声。index で順序確定 |
| `stream_end` | C→S | `id` | これ以上 seg は来ない（残りを flush） |
| `transcript` | S→C | `id`, `index`, `text`, `final` | seg ごとの認識結果（**id=stream, index 追加**） |
| `audio_begin/chunk/audio_end` | S→C | `id=f"{stream}#{index}"`, `parent_id=stream` | seg ごとの音声（**index 順に直列送出**） |
| `stream_stopped` | S→C | `id` | cancel/終端の確定 |

> 代替案（プロトコル最小化）: 既存 `stt_begin/binary/stt_end` を**複数オーバーラップ許可**にして parent でまとめる案もあるが、`stt_begin` は1キャプチャ前提で busy 拒否が絡むため、**新メッセージの方が状態機械が素直**。codex 判断を仰ぐ。

---

## 3. サーバ側パイプライン（`server/orchestrator.py`）

streaming 経路は**専用の状態機械**にし、既存の単発 speak/stt 経路（`_busy` 直列）はそのまま残す（後方互換）。

- **StreamSession（新）**: 1 `stream_id` に対し:
  - `asyncio.Queue` で seg(index, audio) を受信順に受ける。
  - **STTワーカ**: queue から取り出し Qwen `transcribe`（executor）→ `transcript{index}` を送出 → 得たテキストを **TTS出力キュー**へ（index 付き）。先読み深さ `lookahead`（例 1〜2）で seg N+1 を先行 STT。
  - **TTS出力ワーカ（単一・順序直列）**: index 昇順に1つずつ `_stream_tts` 相当で `audio_begin#index → chunks → audio_end`。index k を出し切るまで k+1 は送らない＝**順序保証**。
  - クライアント送信は既存 `_send_lock` で一元化（フレーム交錯なし）。
- **STT精度**: seg=句なので文脈が短い。v1 は許容。任意で「直前 seg テキストを Qwen のプロンプト/文脈に渡す」拡張余地（Qwen3-ASR の prompt 対応次第）。
- **prosody**: seg ごとに TTS が独立＝prosody がリセット。句境界で切る方針で自然さを担保。文中で切れた極小 seg は連結（§1 seg_min_ms）。

**バージイン / cancel**: `cancel{id=stream}` で StreamSession 全体を停止（STTワーカ + TTS出力ワーカ cancel、queue flush）→ `stream_stopped`。クライアントは `PlaybackEngine.request_abort()`（既存 seq 単一所有で安全）。

**多重ストリーム**: 同時に走るのは1 stream（次発話が来たら前を終端 or cancel）。スマホ片手間運用を想定し v1 は単一ストリーム。

---

## 4. クライアント再生（`client/audio/engine.py` 既存流用）

- 既存 PlaybackEngine は **warm stream を発話間で維持しギャップレス連結**（X3）。seg ごとの `audio_begin#index/audio_end` を**到着順に begin→write→end** すれば、seg 間もほぼ無音ギャップなしで繋がる。
- 先頭 seg の audio_begin で `engine.begin`、以降の seg は同一 stream の連続として積む。seq 単一所有で cancel 時に安全に破棄。
- 課題: seg 間で TTS 生成が再生に追いつかないと**アンダーラン（途切れ）**。`lookahead` と seg 長で吸収。要実測（GPT-SoVITS が実時間より速ければ大丈夫）。

---

## 5. フェーズ分割

- **Phase A（土台）**: プロトコル拡張 + サーバ StreamSession（STTワーカ + 順序直列TTS出力）+ クライアント streaming 送出（VAD サブセグメント）。既存単発経路は不変。
- **Phase B（体感調整）**: `seg_silence_ms/seg_max_ms/seg_min_ms/lookahead` を設定化（⚙）し実測チューニング。アンダーラン対策。
- **Phase C（任意・品質）**: seg 文脈プロンプト（STT精度）、prosody連続化の工夫、発話末の全文再STTで字幕だけ訂正（音声は再生済みなので字幕のみ）。

---

## 6. 主要リスク / 要検討（codex レビュー観点）

| 論点 | 内容 |
|---|---|
| 順序保証 vs レイテンシ | TTS出力を index 直列にする＝seg k が遅いと k+1 が待つ。先行 STT で隠せるが、seg k の TTS 自体が遅いと詰まる |
| アンダーラン | seg 間で生成が再生に追いつかない時の無音。lookahead/seg長/プリロールで吸収。要実測 |
| STT精度（句単位） | 文脈不足での誤認識。seg_min_ms で極小片を抑制。文脈プロンプトは Phase C |
| prosody不連続 | seg独立TTSで抑揚リセット。句境界優先で緩和 |
| cancel競合 | STTワーカ/TTS出力ワーカ/queue の協調停止。既存 cancel/seq 機構との整合 |
| 後方互換 | 既存 speak/stt 単発経路（`_busy`直列）を壊さない。streaming は別状態機械 |
| 状態機械の複雑度 | 新プロト26 vs 既存 stt_begin/end 再利用。どちらが保守的か |

---

## 7. 作業順（いつもの手順）

1. 本計画書 → **codex(read-only) レビュー** → 指摘反映（特に §2 プロトコル設計・§3 順序保証・§6 アンダーラン）。
2. Phase A 実装（GLM 委託 or 直接）→ 単体テスト（順序・cancel・アンダーラン擬似）。
3. 実機（docker compose 経由でも可）で体感検証 → Phase B チューニング。
4. branch → commit → `git merge --ff-only` main。

---

## 9. codex レビュー反映（v2 確定仕様）

codex(gpt-5.5, read-only) レビューを実コードで裏取りのうえ反映。**本節が v1 該当部を上書きする確定仕様。** 判定は「下記反映後に Phase A 着手可」。

### 9.1 HIGH① — 音声は「stream 単位で1本」（最重要・v1 の seg 別 audio_begin/end を撤回）

v1 の `audio_begin id="{stream}#{index}"`＋seg別 audio_end は**現行クライアントを壊す**:
- seg0 の `audio_end(id==_playback_id)` で `_finish_stream()`→`_active_id=None`（`ws_client.py:597,606`）→ seg1 が side-band 化。
- `PlaybackEngine.begin()` は毎回 `_seq++`＆carry/resampler リセット（`engine.py:288,176`）、`end()` で tail flush（`:179`）＝**seg 間がギャップレスにならない**。

**確定:** 音声は **stream 全体で1本**にする。
- サーバは `audio_begin{id=stream}` を**1回**（最初の seg 音声が出力可能になった時点）→ 全 seg の PCM を **index 順に連続 binary** で送出 → `audio_end{id=stream}` を**1回**（stream 終端）。
- クライアントは `engine.begin()` 1回 → seg をまたいで `feed()` 連続 → stream 終端で `engine.end()` 1回。**真にギャップレス**。
- seg 境界は**音声フレーミングには出さない**。seg 単位の情報は `transcript{index}`（字幕）とサーバ内部の順序制御だけが持つ。

### 9.2 HIGH③＋並列TTS — 「生成と出力の分離」＋reorderバッファ（順次TTSと複数TTSの共通基盤）

index 直列で「seg k を出し切ってから k+1 を生成」だと、**k+1 の first-chunk ≈270ms が再生の裏に隠れずアンダーラン**（`_stream_tts` は HTTP stream を読み即送出 `orchestrator.py:339` / `tts/client.py:106`）。

**確定: 生成プール → reorder buffer → index 順 output。**
```
STT worker(1) ──index付きtext──▶ 生成プール(N並列) ──index付きPCM──▶ reorder buffer ──▶ output worker(単一)
                                  seg k 再生中に k+1.. を先行生成        next_index だけを      audio_begin/feed*/audio_end
                                                                       1本の音声streamへ
```
- **Phase A = N=1**（seg k を output 中に k+1 を**先行生成してバッファ**）。これだけで TTFA を再生の裏に隠せる。
- **「複数TTS」= N≥2**（複数 GPT-SoVITS worker / マルチGPU）。**同じ reorder buffer のまま生成だけ並列化**＝順序を壊さず後付け可能。＝ユーザ要望「複数TTS次に」の自然な接続点。
- **RTF/アンダーラン実測を Phase A の受け入れ条件に必須化**。ギャップレス条件 ≈ `生成RTF<1` かつ 再生キューに `次seg TTFA+jitter` の余剰。

### 9.3 HIGH④⑤ — StreamSession は別状態機械・idempotent stop・STT cancel の遅延結果破棄

- `self.active_stream_session: StreamSession | None` を **`active_task` とは別**に持ち、legacy `_busy()`（`orchestrator.py:83`）に**含める**。
- `StreamSession.stop()` は STT worker / 生成プール / output worker / queue flush / `stream_stopped` 送信を**一度だけ行う idempotent API**。`_stream_tts` 由来の seg 別 `Stopped` は **stream 経路では抑止**（二重 stopped 回避。既存 `:443` の特殊処理と整合）。
- **STT cancel は executor 内の同期 `transcribe` を止められない**（`:433`）。→ StreamSession に **generation token / cancelled flag** を持たせ、executor 完了後の **結果送信・TTS enqueue を必ず抑止**。**STT worker は1に固定**（Qwen の thread-safety 不明なら複数禁止）。

### 9.4 MED — プロトコル具体化＋ケイパビリティ交渉

- seg の binary 所属を明確化: **`seg_begin{id,index,format}` + binary* + `seg_end{id,index}`**（音声は大きいので b64 は避ける。Phase A の状態機械は「現在の (id,index) に binary を追記、seg_end で確定して STT queue へ」）。
- `ready.features={"speak_stream": true}` を追加（`protocol.py` Ready.features）。**未対応サーバには従来 `utter()` にフォールバック**。
- `Transcript.index: int | None = None` を正式追加（字幕を index 順に UI へ）。余剰 field は現行 Pydantic で無害だが型に入れる。

### 9.5 MED — StreamingVadSegmenter（二段階無音）

既存 `VadSegmenter` は単一 `min_silence_ms` で確定→`reset()`（`vad.py:157`）。**二段階状態が無い**。
- **`StreamingVadSegmenter`（or mode 追加）**: `seg_silence_frames`（短・サブseg確定）と `end_silence_frames`（長・stream_end）を分離。極小片は `seg_min_ms` 未満で次へ連結。
- webrtcvad の 10/20/30ms 制約は既存どおり順守（`vad.py:14`）。**30ms frame では 250ms→実 270ms / 400ms→実 420ms** に丸まる。設定UIには丸め後の実値も表示。

### 9.6 MED — 句単位STTの品質拡張点（型に入れておく）

句分割は誤認識・prosody不連続を招きやすい（`mic.py:171`→単体 transcribe `:433`、文脈なし）。Phase A でも **「前 seg 末尾テキストを次の STT/TTS に渡せる拡張点」を型に用意**（seg_begin or 内部状態に `prev_tail: str` を持てるように）。実利用は Phase C。

### 9.7 MED — クライアント送出API（busy の意味を分離）

`app.py:287` の busy-skip / `ws_client.py:361` の単発排他は **stream 送出を止めてはいけない**。
- **`ws.start_stream() / ws.send_stream_segment(index, pcm) / ws.end_stream()`** を新設。
- GUI の busy は**単発 speak の禁止**には使うが、**mic stream 送出は止めない**（stream はサーバが順序保持）。

### 9.8 Phase A 改訂スコープ＋受け入れ条件

Phase A に必ず含める:
1. 音声 **stream 単位1本**（begin/end 各1回・seg は連続 feed）。
2. seg 別 audio_end で `_finish_stream()` しない（continuous playback）。
3. `StreamSession` を **別状態機械**化・`_busy()` に算入・**idempotent stop（stream_stopped 1回）**。
4. **生成/出力分離（N=1 先行生成バッファ）** ＋ reorder buffer。
5. STT cancelled-flag で **executor 遅延結果を破棄**・STT worker=1。
6. `StreamingVadSegmenter`（二段階無音）。
7. `ready.features.speak_stream` ＋未対応フォールバック。
8. **FakeTTS で RTF/アンダーラン/順序/cancel を擬似検証**（FakeStream に生成遅延を注入できる seam）。

### 9.9 総合判定

中核（増分STT→順序出力・VADクライアント据え置き）は妥当。上記 v2 反映後に **Phase A 着手可**。「複数TTS(N≥2)」は §9.2 の reorder buffer 上の拡張として Phase A 後に着手。

---

## 8. スコープ外（今回やらない）

- 真の再デコード型ストリーミングSTT（成長音声を N ms ごとに再 transcribe し安定プレフィックス確定）。GPU を TTS と共有する以上 v1 は重い → 将来オプション。
- 複数同時ストリーム / 多人数。
- LLM 中継（入力ソースを LLM 出力にする拡張は本設計の seg を text セグメントに差し替えれば流用可だが今回は対象外）。
