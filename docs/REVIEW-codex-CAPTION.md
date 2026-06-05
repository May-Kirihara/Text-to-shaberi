# REVIEW-codex-CAPTION — 配信字幕オーバーレイ 計画書レビュー

- レビュア: codex (gpt-5.5, read-only)
- 対象: `docs/IMPL-CAPTION.md`（実装前の計画）＋ `ws_client.py` / `audio_monitor.py` / `config.py` / `app.py` / `protocol.py`
- 反映先: `docs/IMPL-CAPTION.md` 末尾「codex レビュー反映（v2）」

## 判定: GO-with-fixes

### 1. [NO-GO] 再接続後に消去イベントを取りこぼす
`_cur` に clear を保持しつつ mid-join では `visible:true` のみ送信。切断中に発話終了するとブラウザ上の古い字幕が残り続ける。
**修正:** `_cur` は `visible:false` も保持し、新規購読者へ常に送る。初期状態のみ `_cur=None`。

### 2. [NO-GO] `seq` がプロセス再起動で巻き戻る
ブラウザページを残したままアプリ再起動すると、`lastSeq` より小さい新イベントが永久に破棄される。
**修正:** イベントへ起動ごとの session_id を付与し変更時 `lastSeq=-1`。最低限 WebSocket `onopen` ごとにリセット。

### 3. [NO-GO] text-stream の `await` 越し競合
`_ws_send()` 中に recv loop が動き、`AudioEnd`/cancel/disconnect が `_finish_stream()` を実行し得る。同一ループでも競合は存在。終了後に古い segment を append すると次発話へ混入。
**修正:** `sid` を保存し、送信成功後に `self._active_id == sid and self._stream_mode` の時だけ append。append は送信成功前に行わない。`start_text_stream()` で buf リセットは妥当。

### 4. [MED] mailbox は安全だが wire envelope が欠落
`payload` だけを pending にすると仕様上の `"event":"caption"` が送信されない。
**修正:** `broadcast()` で `snap = {"event": event, **payload}` を生成し `_cur`/pending に。sender の pending 取得から `event.clear()` まで await を入れなければ latest-wins は安全。mid-join も pending 経由の一経路だけに限定。

### 5. [MED] Upgrade 判定が厳しすぎる
`.lower() == "websocket"` は複数値を扱えない。値は token 分解が必要。
**修正:** `Upgrade` の全値をカンマ分割して比較。または `/ws` の正規ハンドシェイクだけ `None`、通常 `GET /` だけ `Response`、POST/未知パスは 405/404。`Response` コンストラクタ形式は計画どおり。

### 6. [MED] 終了一回性は成立するがテストを必須化
`_caption_open` を既存 monitor パターンと同じ場所で閉じれば一回性は成立。AudioBegin 受理も `_playback_id is None` ガード内なので capture 併発・二重 begin は問題なし。
**修正:** AudioEnd/Stopped/StreamStopped/Error/cancel応答/disconnect/送信失敗/format不正/空stream をパラメータ化し「begin 最大1回・clear ちょうど1回・begin 無しなら clear 無し」を検証。

### 7. [MED] WS の Origin 制限が必要
loopback でも悪意ある Web ページから `ws://127.0.0.1:8772` に接続され字幕内容を読まれ得る（CSWSH）。`textContent` は DOM 注入対策として正しい。
**修正:** ブラウザ接続では `Origin` を自サーバ由来に限定。非ブラウザ用途を許すなら Origin 無しを明示的に許可。token 比較は `hmac.compare_digest()`。`/ws` 以外を拒否。

### 8. [MED] 字幕全文が無制限に増える
長時間 text-stream では `_caption_buf` と毎回の全文 JSON 生成が無制限。
**修正:** `max_chars` を設け超過時は先頭から切り詰める。テストに高頻度 coalesce、clear 直後の show、送信中更新、mid-join clear、購読者二重 drop を追加。
