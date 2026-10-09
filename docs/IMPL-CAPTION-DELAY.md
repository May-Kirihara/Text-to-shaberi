# IMPL: 字幕オーバーレイの「遅延表示（配信ディレイ同期）」

## 目的 / 要件

配信字幕オーバーレイ（`[caption]` / 既定 8772）で、字幕の**表示も消去もまるごと一定時間後ろへずらす**機能を足す。
OBS や配信プラットフォームの遅延（ストリームディレイ、映像遅延フィルタ）に**字幕タイミングを合わせる**用途。

- 要件確定（May, 2026-06-06）:
  - 挙動 = **配信ディレイ同期**（表示・消去を同じ delay でずらす ⇒ 表示している長さ＝相対タイミングは不変）
  - 遅延量の指定 = **URLクエリ** `?delay=<ミリ秒>`（例 `http://127.0.0.1:8772/?delay=2000`）
- 非対象（今回やらない）: 「余韻（喋り終わり後に残す）」「タイプライター/段階表示」。将来別タスク。

## 設計方針：**ブラウザJS側だけで完結**（Python 側は無変更）

字幕イベントの生成・中継経路（`ws_client._emit_caption` → `app._on_caption` → `CaptionServer.broadcast`
→ latest-wins mailbox → ブラウザ WS）は**一切触らない**。遅延は同梱HTMLの受信→描画の間に**遅延ライン（FIFOキュー）**
を1段挟むだけで実現する。

理由:
- 表示も消去も**同じ delay** でずらすので、Python 側で時刻管理する必要がない（描画の手前で一定遅延を入れれば十分）。
- サーバの latest-wins coalesce は実発話では表示/消去が秒オーダーで離れているため遅延ラインに影響しない
  （coalesce が捨てるのは localhost 上で sub-ms に重なった中間スナップショットのみ＝今と同じ挙動）。
- ソースごとに別 delay が要るケース（複数 OBS ソース）にも URL クエリだけで対応でき、HTML 再読込不要。

### 遅延ラインの共通ロジック（3テンプレート共通で同じ意味）

```js
// ?delay=2000 → 表示/消去を 2000ms 後ろへずらす（配信ディレイ同期）
var DELAY = (function () {
  var n = parseInt(new URLSearchParams(location.search).get('delay'), 10);
  if (!isFinite(n) || n < 0) n = 0;
  if (n > 60000) n = 60000;          // 上限 60s（暴走/タイプミス保護）
  return n;
})();

var queue = [];                       // [{due, msg}] 受信順 FIFO
var timer = null;

function pump() {
  timer = null;
  var now = performance.now();
  while (queue.length && queue[0].due <= now) apply(queue.shift().msg);
  if (queue.length) timer = setTimeout(pump, Math.max(0, queue[0].due - performance.now()));
}

function enqueue(m) {
  if (DELAY <= 0) { apply(m); return; }            // delay 0＝従来どおり即描画（後方互換）
  queue.push({ due: performance.now() + DELAY, msg: m });
  if (timer === null) pump();
}
```

接続部の差分は `ws.onmessage` が `apply(...)` ではなく **`enqueue(...)`** を呼び、`ws.onopen` で
**キューをフラッシュ**する点だけ。`apply()`（seq ガード＋ textContent 注入＋クラス切替）は**現状のまま不変**。

```js
ws.onopen = function () {       // 再接続: 旧接続由来の未描画イベントを捨て、_cur 再送で遅延付き再同期
  queue.length = 0;
  if (timer !== null) { clearTimeout(timer); timer = null; }
  lastSeq = -1;
};
ws.onmessage = function (e) { try { enqueue(JSON.parse(e.data)); } catch (_) {} };
```

### 設計上の判断（codex レビュー反映済み）

1. **再接続(`onopen`)でキューをフラッシュする**（codex#3 反映）。旧接続の未描画イベントを残すと、
   接続断中に未受信だった `clear` が「再接続時刻+delay」で遅れて届き字幕が居残る。フラッシュ後は
   サーバの mid-join `_cur` 再送が唯一の真実源となり、遅延付きで現在状態へ再同期する（show なら遅延再表示、
   clear なら遅延消去）。`_cur` は常に最新スナップショットを保持するので恒久的な取りこぼしは起きない。
2. **単一接続中は FIFO=受信順=seq 昇順**。`apply` の seq ガードは従来意味のまま（サーバ再起動で seq 逆転時の保険）。
   タイマー発火順に依存しない（単一スケジューラが常に先頭 due を比較）。フラッシュにより接続跨ぎの順序矛盾は発生しない。
3. **キュー無限増殖なし**。各要素は delay（最大60s）後に必ず drain。発話単位の低レートなので最大でも数百要素。
4. **`performance.now()` 単調時刻**を使用（OBS CEF=Chromium で利用可、`Date.now()` の巻き戻り影響を受けない）。
5. `DELAY<=0`（クエリ無し/0/不正）は `apply` 直行で**完全な後方互換**（既存テスト・既存ユーザーに影響ゼロ）。
6. **vtuber.html は元々 `visible:false`（消去）を適用しない**プリセット（直近テキストを保持し続ける仕様）。
   遅延ラインは vtuber でも全 `apply` を一律 delay するので、**テキスト切替（swap）が delay 分ずれる**＝
   配信ディレイ同期は成立する。ただし「消去の遅延」は vtuber には存在しない（元から消えないため）。
7. **`?delay=2000&token=...` の併用可**。既存実装は `location.search` を `/ws` にそのまま転送するため、
   token 認証環境でも `?delay=2000&token=xxx` の形で共存する（`delay` はサーバ認証では無視される）。

## 変更ファイル

| ファイル | 変更 |
|---|---|
| `src/text_to_shaberi/client/caption/index.html` | 遅延ライン追加（`apply` は不変）＋先頭コメントに `?delay=` 追記 |
| `src/text_to_shaberi/client/caption/vtuber.html` | 同上（var 版に合わせて記述） |
| `src/text_to_shaberi/client/caption_server.py` | `_INLINE_HTML`（fallback）に同ロジックを minify して追加 |
| `docs/USECASES.md` / `README.md`（任意） | 「配信字幕オーバーレイ」節に `?delay=` の使い方を1〜2行追記 |
| `client.toml.example`（任意・コメントのみ） | `[caption]` に「URLで ?delay=ms 指定可」の注記 |

Python ロジック（`CaptionServer` / `ws_client` / `config`）と `[caption]` の TOML スキーマは**無変更**。

## 既知の限界（codex#1,#2 反映・正直な割り切り）

ブラウザ側遅延ライン方式は「**OBS ソースが配信中ずっと接続したまま**」という通常運用を前提とした best-effort：

- サーバは latest-wins mailbox + `_cur`（**履歴ではなく最新スナップショットのみ**）。よって、ソースが
  **発話の途中で新規接続/再接続**すると、復元できるのは「現在状態（遅延付き）」だけで、過去 delay 秒間の
  show→clear 履歴は再現できない。配信開始前に OBS ソースを開いて繋ぎっぱなしにする通常運用では問題にならない。
- 同様に latest-wins の coalesce は、購読者の send が発話全体に渡ってブロックした場合のみ短い字幕を落とし得る
  （localhost・単一購読者では実質起きない＝現状と同じ特性）。
- これらを「ソース別 URL delay」を保ったまま厳密化するにはサーバ側に時刻付きリングバッファ履歴が必要だが、
  要件（URLクエリでソース別 delay）に対し過剰なので**今回は採らない**。将来必要になったら別タスク。

## 受入基準 / 検証

- `?delay=2000` を付けたソースで、ローカル再生より**約2秒遅れて**字幕が表示され、**同じく約2秒遅れて**消える
  （＝表示時間は遅延なしと同じ）。vtuber は **2秒遅れてテキストが切り替わる**（消去は元々無し）。
- クエリ無し（`/`）は**従来どおり即時**表示/消去（後方互換）。`?delay=0`・不正値も即時。
- 既存 pytest 緑のまま（HTML 構造マーカー `#cap.show` / `--pop-ms`、`startswith(b"<")` を壊さない）。
- **新規 pytest**: index/vtuber/inline の3 page bytes すべてに遅延ロジック（`delay` パース）が含まれることを確認
  （3テンプレの同期ズレ＝片方だけ実装漏れの回帰を防止）。
- **JS 構文検証**: 各テンプレ／`_INLINE_HTML` から `<script>` を抽出し `node --check` で構文エラーが無いこと
  （pytest/ruff は JS 構文を見ないため・codex#10,#11 反映）。
- ruff 緑（Python 変更は `_INLINE_HTML` 文字列のみ）。
- 実ブラウザ smoke: `?delay=3000` で 3 秒の表示遅延・消去遅延を目視。複数タブで別 delay が独立に効く。

## 追補 v2: UI 設定から遅延を変更（起動中 OBS にも生反映, 2026-06-06)

要件追加（May）: 遅延を **GUI の ⚙設定からいじれる**ように。反映タイミングは **起動中の OBS ブラウザソースにも生反映**（リロード不要）。client.toml にも保存し次回起動でも有効。

設計（coalesce / seq 安全・URL クエリのソース別上書きは維持）:
- 遅延の既定値（＝UI 設定値）を **`CaptionServer.broadcast` が全字幕イベントの payload に `delay` として載せる**。latest-wins mailbox は coalesce するので「最新スナップショット＝最新 delay」が常に成立＝取りこぼし不可。
- ブラウザ JS は各メッセージから `srvDelay` を取り込む（**seq ガードより前**＝再配送が seq 据え置きでも delay は確実に反映）。`?delay=`(=`urlDelay`) があればそれを最優先（ソース別上書き）。
- UI 変更時は `app.on_settings` → `cfg.caption.delay_ms` 更新 → **`caption_server.set_delay()`** が現在状態(`_cur`)を新 delay 付き・seq 据え置きで再配送＝接続中 OBS へ即時反映。`_cur` が None（未発話）の間は属性更新のみで、次の broadcast / mid-join が運ぶ。
- 優先順位: **URL `?delay=` > UI/config 既定 > 0**。

変更ファイル追加: `config.py`(`CaptionConfig.delay_ms` + 0..60000 クランプ)、`caption_server.py`(`delay_ms`/`_clamp_delay`/`broadcast` 注入/`set_delay`)、`app.py`(構築時 `delay_ms=` + `on_settings` で `set_delay`)、`ui/settings_dialog.py`(「字幕」グループに delay SpinBox + `values()["caption"]`)、`client.toml.example`、3 JS テンプレ(`urlDelay`/`srvDelay`/`curDelay` + due 昇順キュー)。

### codex レビュー round2 反映（実装差分レビュー）
- サーバ `set_delay` と mailbox/`_closing`/drain/二重 close の相互作用 = **問題なし**（低）。seq 据え置き再配送の冪等性・優先順位・後方互換も設計どおり（低）。
- **【高・修正済】JS キューが受信順 FIFO 前提**だと、delay を**短縮**した時に「後着で due が早い再配送」が末尾に入り、古い先頭の due（最大60s後）まで pump が起きず生反映が詰まる。→ **キューを due 昇順の挿入（簡易プライオリティキュー）に変更**し、先頭が変わったらタイマー張り直し。短縮時に遅れて発火する古い項目は **seq ガードで破棄**されるため字幕の蘇りも無し（node シミュレーションで確認: 短縮が ~新delay で反映・stale 蘇り 0）。
- 既知の弱さ（低・据え置き）: `load_client_config` は `delay_ms = "abc"` 等の非int TOML で比較例外になり得る（既存 int 項目=port/max_chars 等と同じ系統的特性のため一貫性優先で据え置き。実際に JS へ流れる値は server 側 `_clamp_delay` が try/except int で防御済み）。

検証追加: pytest（caption broadcast delay 注入 / set_delay 再配送・クランプ / config delay クランプ / settings_dialog values）107 passed・ruff 緑・node --check 3JS OK・短縮シナリオ node sim PASS。

## やらないこと（スコープ外・将来）

- 余韻（喋り終わり後の保持）、タイプライター/段階表示、UI ボタンURLへの delay 付与（payload 追従で不要）。
