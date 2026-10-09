# IMPL: 字幕消去を「実際の再生終了」に同期（タイマー方式）

## 背景 / 症状

実機で「字幕の表示時間が実際の発話の長さと一致しない（早く消える）」。

## 根本原因（コードで確認済み）

字幕は **AudioBegin で表示・AudioEnd→`_finish_stream` で消去**。だが AudioEnd は
**サーバが音声データを送り終えた**タイミング。ローカル再生は engine/sink の worker スレッドが
**デバイスへのブロッキング書き込み（≒実再生レート）**でキューを消化し、`end()` は終端 sentinel を
積むだけで即 return（非ブロッキング契約）。よって AudioEnd 時点ではまだ大量の音声がバッファに残り、
**字幕が喋り終わりより数秒早く消える**。表示側は `prime_ms` 既定0でほぼ実再生開始と同時なので、
ズレの主因は**消去側**。

## 方針（May 確定: 「消去を実再生終了に同期」）

**復号後 PCM の総バイト数から音声の実長を算出**し、`AudioEnd` 受信時に「再生がまだ残っている分 ＋
わずかな余白」だけ**字幕消去をタイマーで遅延**する。表示開始は現状維持。

### なぜ worker 完了コールバックでなくタイマー方式か（codex レビュー round1 反映）

完了コールバック方式（engine/sink の worker が `_End` 到達時に `call_soon_threadsafe` で通知）は、
codex が複数の **高** リスクを指摘:
- engine 常駐 reuse での tag/seq 相関、stale `_End`、abort 時非通知の funnel 条件、
  defer 中の `_active_id/_playback_id/_sink` リセットとの相関、二重消去/timer リーク。

タイマー方式はこれを**全て回避**:
- 完了通知をスレッド跨ぎで受けない（**worker/engine/sink は無変更**）。
- 消去ロジックは **ws_client の recv ループ（単一スレッド）内で完結**。`loop.call_later` のみ。
- **タイマー自体が消去機構**なので「完了通知が来ず字幕が消えない」（前回不具合）の再発が原理的に無い。

トレードオフ: 精度は「実デバイス書き込み完了」よりわずかに落ちる（再生開始の見積り誤差・device latency）。
ただしズレの主因（秒オーダーの早消去）は解消し、本アプリの通常運用（連続再生バックログが稀）で十分。
**既知の限界**: 直前発話の再生が押している（バックログ）状態で次が始まると見積りがずれ得る（稀・後述）。

## 設計（ws_client.py のみ変更。engine.py / sink.py は無変更）

### 新フィールド
- `_caption_pid`: 現在表示中字幕の playback id（AudioBegin で設定）。
- `_audio_bytes`: 当該発話の復号後 PCM 累積バイト数。
- `_decoded_sr` / `_decoded_ch`: 復号後フォーマット（再生実長算出用）。
- `_first_chunk_t`: 最初のチャンクを feed した時刻（≒再生開始の代理）。
- `_caption_clear_timer`: 遅延消去の `TimerHandle`（無ければ None）。

### 既存フローへの差し込み
- **AudioBegin（表示・811付近）**: `_cancel_caption_clear_timer()` →
  `_caption_pid = msg.id`、`_audio_bytes=0`、`_first_chunk_t=None`、`_decoded_sr/_decoded_ch` 記録、
  `_caption_open=True`、show emit（既存）。
- **`_on_binary`（非 capture の feed 直前）**: `if _first_chunk_t is None: _first_chunk_t=loop.time()`、
  `_audio_bytes += len(pcm)`。
- **AudioEnd 正常パス（874-879・非cancel/非capture/再生中のみ）**:
  `engine/sink.end()`（既存）後に **`_finish_stream(defer_caption_clear=True)`** ＋
  **`_schedule_caption_clear(msg.id)`**。
- **`_finish_stream(defer_caption_clear=False)`**: `defer` かつ `_caption_open` の時だけ字幕 emit clear を
  スキップ（`_caption_open`/`_caption_pid` を残す）。それ以外は**即消去（従来どおり）＋
  `_cancel_caption_clear_timer()`**。他状態リセット（monitor/tap/active_id/playback_id/sink/…）は不変。
  既存の全呼び出しは defer 既定 False＝即消去のまま（cancel/capture/zero-audio/Stopped/Error/STTのみ）。

### 遅延消去ロジック（全て recv ループ＝単一スレッド上）
- `_schedule_caption_clear(pid)`:
  ```
  bps = _decoded_sr * _decoded_ch * 2            # pcm_s16le
  total_ms  = (_audio_bytes / bps * 1000) if bps>0 else 0
  start_t   = _first_chunk_t or loop.time()
  clear_at  = start_t + (prime_ms + PREROLL_EST_MS + total_ms + END_MARGIN_MS)/1000
  delay     = max(0, clear_at - loop.time())
  _cancel_caption_clear_timer()
  _caption_clear_timer = loop.call_later(delay, _do_caption_clear, pid)
  ```
  定数: `PREROLL_EST_MS≈200`（再生開始の食い込み補正）, `END_MARGIN_MS≈150`
  （device latency＋ごく短い余韻。**正方向**＝わずかに残ってから消す＝早消えを防ぐ）。
- `_do_caption_clear(pid)`: `_caption_clear_timer=None`;
  `if _caption_open and pid==_caption_pid: _caption_open=False; _caption_pid=None; emit clear`。
- `_cancel_caption_clear_timer()`: `if timer: timer.cancel(); timer=None`。
  **AudioBegin / 即消去 `_finish_stream(False)` / `cancel()` / disconnect・close** で必ず呼ぶ（リーク/誤発火防止）。

### A→B 連続発話の安全性（codex (1)）
`_do_caption_clear` は `pid==_caption_pid` ガードで、A の遅延消去が B 表示中に発火しても**no-op**。
さらに B の AudioBegin で A のタイマーを cancel。`_caption_pid` は AudioBegin でのみ更新＝表示中字幕と一意対応。

### 中断時の即消去（codex (4)）
cancel / error / opus decode 失敗 は `_finish_stream(defer=False)`（即消去）。その中で
`_cancel_caption_clear_timer()` も呼ぶ。abort 時に遅延消去が宙に浮かない。
（opus decode 失敗で sink 使用時の `self._sink.abort()` 欠落＝既存の非対称はついでに対称化。）

### codex round2 で判明した寿命管理の穴（NO-GO→修正）— 共通ヘルパで塞ぐ

`_clear_caption_now()`: `_cancel_caption_clear_timer()` ＋ `if _caption_open:` 即 emit clear＋reset。
これを以下の**全経路**で呼ぶ:

- **高: overload abort（ws_client.py:734）** は `feed()` 失敗時に engine/sink.abort＋Cancel 送信のみで
  `_finish_stream()` を呼ばない。→ その場で `_clear_caption_now()` を追加（サーバ応答に依存せず字幕を消す）。
- **高: 切断/再接続/close 後の defer 残り**（defer 正常終了後は `_active_id=None`・字幕表示中・timer pending）。
  recv ループ finally（694）は `_active_id` がある時しか finish しない → **finally と `_teardown_ws`(close/reconnect 共通)
  に `_clear_caption_now()` を無条件追加**。timer も caption も宙に残さない。
- **高: `cancel()`（587）が `_active_id is None` で早期 return**。defer 中は active 無しでも字幕表示中。
  → cancel を「active 無しでも `_caption_open` なら engine/sink.abort＋`_clear_caption_now()`」に拡張
  （ユーザーの stop で遅延字幕＆残再生を止められる）。

### 見積りの堅牢化（codex round2 中）
- **duration snapshot**: `total_ms/start_t/pid` と delay を **`_finish_stream` を呼ぶ前**に算出（local 変数化）。
  将来 `_finish_stream` が新フィールドも reset しても delay が 0 化しない。
- **prime_ms は backend 別**: 常駐 `_engine` は prime_ms 非使用（prewarm/keepalive）。`_sink` のみ先頭無音に使う。
  → `prime_component = 0 if _engine is not None else _prime_ms`（engine 経路で過大遅延しない）。
- **`_audio_bytes`/`_first_chunk_t` は feed 成功後に更新**（失敗 chunk/abort を相関に混ぜない）。
- channels: `total_ms = bytes/(decoded_sr*decoded_ch*2)` は**音声データの実長**で、engine の downmix 有無に
  非依存（frame 数＝bytes/(ch*2) は不変）。decoded_sr/decoded_ch は AudioBegin の復号後値を使用。

## 受入基準 / 検証
- 高速TTSでも字幕の表示時間 ≒ 実発話時間（喋り終わりとほぼ一致・むしろ ~150ms 残ってから消える）。
- cancel / capture / 空テキスト / エラー / STT のみ は従来どおり即消去（回帰なし）。
- 連続発話 A→B で A の遅延消去が B を消さない（pid ガード＋cancel）。
- disconnect/再接続でタイマーが宙に残らない。
- 単体テスト: `_schedule_caption_clear` の delay 計算、pid ガード、即消去経路の timer cancel、
  AudioEnd 正常 vs cancel/capture の分岐。FakeClock/手動 loop で検証。

## レビュー
字幕の寿命/キャンセル変更のため codex レビュー（[[codex-review-self-authored-concurrency]]）。
タイマー方式は単一スレッドで完結し、round1 の高リスク（スレッド相関）を解消済み。round2 で GO 確認。

## codex 実装レビュー結果（diff レビュー）
**条件付き GO**。round2 の修正条件 6 点（overload 即消去 / defer 中 cancel・close・reconnect の明示 clear /
duration snapshot / prime backend 別 / feed 成功後加算 / pid ガード＋timer leak なし）はすべて正しく反映を確認。
残指摘 2 点はコメント精度修正＋限界明文化で対応（下記）。実用パス（opus=mono＋常駐 engine）は GO。

## 既知の限界（許容）
- **PCM stereo**: engine は `channels>1` を実質 mono 処理する既存仕様のため、stereo PCM passthrough だと
  見積り（stereo 実長）と再生（mono 解釈）がズレ得る。既定の opus は常に 48k/mono で正確。stereo PCM は
  元々 engine 側で非対応のため許容（mono 前提）。
- **sink fallback の残再生**: defer 中 cancel で字幕は即消去できるが、`_finish_stream` が `_sink=None` に
  しているため sink 経路の残再生は止め切れず drain する（常駐 engine 経路は abort 可）。既存挙動。
- バックログ（直前再生が押している時の次発話）の厳密同期＝タイマー方式の既知限界として許容。

## やらないこと
- 表示開始の実再生同期（May は消去のみ選択）。capture の同期（手元再生なし）。

## ホールド時間（余韻）の追加（dwell + replace）

### 背景
「実再生終了 + 150ms（END_MARGIN）」で消えても、**話速（約6字/秒）＞読み速度（約4字/秒）**のため
長文ほど視聴者が読み終える前に字幕が消える（読み残し）。放送字幕の「1秒4字」則は翻訳字幕
（聞き取れない外国語を読む）向けの値で、同言語の TTS 補助字幕には長すぎる。そこで表示時間を
伸ばすのではなく**ホールド型（dwell + replace）**で解決する: 発話の実再生終了後、字幕を文字数に
応じた時間だけ残し（dwell）、**次の発話が来たら即座に置換**（replace）する。

「次の字幕が来たら即置換」「ユーザー停止・エラーは即消去」は既存機構（AudioBegin が clear timer
を cancel して新字幕表示＋pid ガード、`_finish_stream(defer=False)` の即消去）で既に成立している。
本変更は消去タイマーの遅延にホールド分を**加算するのみ**。

### 数式
`_compute_caption_clear_delay()`（AudioEnd 正常パスで `_finish_stream` 前に呼ぶ＝`_caption_buf` 生存）:

```
n       = len("".join(_caption_buf))     # max_chars 折りたたみ済み＝実際の表示文字数
hold_ms = min(hold_base_ms + n × hold_char_ms, hold_max_ms)
clear_at = start_t + (prime_component + _CAPTION_PREROLL_EST_MS(200)
                      + total_ms + _CAPTION_END_MARGIN_MS(150) + hold_ms) / 1000
delay   = max(0, clear_at - now)
```

`hold_max_ms < hold_base_ms` の場合は上限が優先（`min()` 挙動）。ホールド無効化（従来動作）は
`hold_base_ms = 0, hold_char_ms = 0`（hold=0）。

### 既定値の意味（2000ms + 20ms/字, 上限 5000ms）
- `hold_base_ms = 2000`: 短発話（あいづち等）でも2秒は残す。読み手の認知遅れのベース分。
- `hold_char_ms = 20`: 長文ほど読み残しが増えることへの線形補正（1文字あたり）。
- `hold_max_ms = 5000`: 無発話時間に字幕が貼り付くのを防ぐ上限。既定だと 150字前後で飽和
  （2000 + 20n ≧ 5000 → n ≧ 150）。

### TOML 設定例（`[caption]` 節）
```toml
hold_base_ms = 2000   # 発話終了後も字幕を残す基本ホールドms（完全無効化は base=0 かつ char=0）
hold_char_ms = 20     # 文字数連動: 表示1文字あたりの追加ホールドms
hold_max_ms  = 5000   # ホールド合計の上限ms（次の発話が来たら即置換・停止は即消去）
```
3キーとも `delay_ms` と同一の 0..60000 クランプ（`config.py` `load_client_config`）。
WSClient 生成は `app.py` が `cfg.caption.hold_*` を渡す（constructor 既定値は CaptionConfig と同一）。

### 既知の非対象
- **STT-only（音声なし Transcript 終了）は即消去**のまま（ホールド対象外）。
- **SRT 字幕**（`srt_player.py` 経由）は本機構と無関係のため対象外。
- **設定ダイアログUIは無し**（TOML のみ）。設定ダイアログは caption 内 `delay_ms` キーのみを
  部分更新するため、`hold_*` キーが設定保存で欠落・破壊されることはない。
- **stop / cancel / error / 切断は従来どおり即消去**（ユーザー明示停止・異常系に余韻を残さない）。
  なお『即消去』は WS 通知の意味で、`delay_ms`（配信ディレイ同期）使用時はブラウザ側キューの
  遅延後に表示へ反映される（表示/消去とも一様に遅延するため相対タイミングは不変）。
