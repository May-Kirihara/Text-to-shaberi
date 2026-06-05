# IMPL-CAPTION — 配信字幕オーバーレイ（ブラウザソース型）

## 目的（エンドユーザ要件）

配信向けに「今喋っている内容」を**別ウインドウ**で字幕として流す。
- **背景抜きしやすい**（OBS でアルファ透過、グリーンバック不要）
- **任意のフォント＆縁取りなどの装飾**ができる
- 今話している内容を**音声同期**で表示する

## 確定した設計判断（ユーザー合意済み）

| 項目 | 決定 | 理由 |
|---|---|---|
| 描画方式 | **ブラウザソース型**（ローカル HTTP+WS → HTML/CSS、OBS Browser Source） | CSS で縁取り/フォント/アニメが完全自由・透過がネイティブ。既存の localhost サーバ（8770/8771）と同じ流儀 |
| 背景キー | **透過（アルファ）** | OBS Browser Source は透過が標準。グリーンスピル無し |
| 同期 | **音声同期**（`AudioBegin` で表示 / `AudioEnd` で消す） | 「今話してる内容」に忠実 |
| speak / STT | 1発話＝1テキストを音声同期で差し替え | サーバが begin/end を1回ずつ出すので完璧に同期可能 |
| text-stream（AITuber） | **音声ゲート付き蓄積**（鳴り始めで表示開始→文が届くたび追記→鳴り終わりで消去） | LLM はテキスト生成が音声合成より圧倒的に速い。差し替えだと字幕が音声を追い越す（先走り）。蓄積なら「今喋ってる答え」が破綻なく一望でき、音声ゲートで自然に消える |

## サーバ音声フレーミング（調査済み・設計の前提）

`server/orchestrator.py` 確認結果。**これが音声同期の単位を決める**:

| モード | `AudioBegin` / `AudioEnd` | id / parent_id | 文ごとの音声境界信号 |
|---|---|---|---|
| **speak** | 全体で1回ずつ | `id=speak_id, parent_id=None` | — |
| **STT/utter (+auto_tts)** | `Transcript` → 1回ずつ | `id="tts-{stt_id}", parent_id=stt_id` | — |
| **text-stream (mode=text)** | **ストリーム全体で1回ずつ**（全セグメント連結） | `id=stream_id, parent_id=None` | **無し** ⚠️ |

→ **text-stream はサーバが「今どの文を喋っているか」を一切返さない**。クライアントが知るのは自分が送った `SegText`（送信タイミング）だけ。
→ だから text-stream の per-segment 音声同期は**原理的に不可能**。よって「音声ゲート＋クライアント側テキスト蓄積」を採用する（上表）。

> ⚠️ この制約は将来も誤解されやすい。`docs/HANDOVER.md` §7（アーキ不変条件）と GaOTTT にも記録する。

## アーキテクチャ（既存パターンの踏襲）

`AudioMonitorServer`(8771) の broadcast 構造をほぼ流用。差分は **(a) バイナリ無し（小さな JSON 制御イベントのみ）**、**(b) 静的 HTML 配信を相乗り**。

```
ws_client（recv loop / 同一 qasync ループ）
  ├ AudioBegin(受理)  → _emit_caption("caption", {text, visible:true})   ← 表示開始
  ├ send_text_segment → buf.append; open中なら _emit_caption(update)      ← 蓄積追記
  └ _finish_stream    → _emit_caption("caption", {text:"", visible:false}) ← 消去
        │  (best-effort tee・例外隔離。monitor と同じ流儀)
        ▼
app._on_caption(event, payload) → CaptionServer.broadcast(event, payload)
        │
        ▼
CaptionServer（127.0.0.1:8772）
  ├ HTTP GET（非WSリクエスト）→ 字幕HTMLページを返す（process_request）
  └ WS（/ws へ upgrade）→ 購読者へ caption イベントを push
        │
        ▼
OBS Browser Source（http://127.0.0.1:8772/）
   page.js が ws://<host>/ws に接続し、caption イベントで textContent を更新（CSS装飾）
```

### なぜ単一ポートで済むか（websockets 16）

`websockets.serve(handler, host, port, process_request=cb)`。`cb(connection, request)`:
- `request.headers.get("Upgrade","").lower() == "websocket"` → `return None`（WS ハンドシェイクへ）
- それ以外（普通の HTTP GET）→ `Response(200, "OK", Headers(...), body=html_bytes)` を返してページ配信

`Response(status_code:int, reason_phrase:str, headers:Headers, body:bytes=b"")`（websockets.http11）。

## 字幕イベントプロトコル（Python → ブラウザ、WS 上の JSON）

**単一イベント型のスナップショット**（冪等・seq で順序保証）:

```json
{"event": "caption", "text": "<現在の全文>", "visible": true,  "seq": 12}
{"event": "caption", "text": "",            "visible": false, "seq": 13}
```

- `visible:true` → `text` を表示（フェードイン）。`visible:false` → 隠す（フェードアウト）。
- `seq` は変化ごとに単調増加。ブラウザは古い seq を捨てる（順序逆転対策）。
- **mid-join（OBS 再接続）**: 新規購読者には接続時に「現在の状態」(`_cur`) を送る。発話中なら途中字幕がすぐ出る（monitor の `_cur_format` と同思想）。
- ブラウザは **`textContent` でセット**（`innerHTML` 厳禁＝喋り内容の HTML/スクリプト注入防止）。

> 設計上スナップショット（全文）なので、購読者キューは **deque ではなく「最新1件のみ保持（latest-wins mailbox）」** にできる。
> → monitor のような stall-drop の複雑さが不要。遅い購読者は最新状態だけ受け取る。メモリリーク不可能。

## 実装詳細

### A. `src/text_to_shaberi/client/ws_client.py`

**コンストラクタ**: `on_caption: Callable[[str, Any], None] | None = None` を追加。

**状態（`__init__`）**:
```python
self._on_caption = on_caption
self._caption_open: bool = False
self._caption_buf: list[str] = []
self._caption_seq: int = 0
```

**ヘルパ**（monitor の `_emit_monitor` と同型・例外握り）:
```python
def _emit_caption(self, text: str, visible: bool) -> None:
    if self._on_caption is None:
        return
    self._caption_seq += 1
    try:
        self._on_caption("caption", {"text": text, "visible": visible, "seq": self._caption_seq})
    except Exception:
        self._log("caption emit 失敗")
```

**テキスト元での buf セット**:
- `speak(text, ...)`: active 設定後 `self._caption_buf = [text]`（pending。表示は AudioBegin）。
- `start_text_stream(...)` / `start_stream(...)`: `self._caption_buf = []`（新発話で確実にリセット）。
- `send_text_segment(index, text)`: `self._caption_buf.append(text)`; `if self._caption_open: self._emit_caption("".join(self._caption_buf), True)`。
- `Transcript`（非 stream_mode 分岐）: `self._caption_buf = [msg.text]`（pending。AudioBegin が後続）。
  - stream_mode 分岐（mic streaming）: `self._caption_buf = [msg.text]`; `if self._caption_open: self._emit_caption(...)`（暫定。主用途外）。

> すべて同一 qasync ループ上（send_text_segment は TextStreamBridge consumer タスク、AudioBegin は recv loop）。スレッド競合無し。

**AudioBegin（受理分岐、monitor begin emit の直後・capture 分岐の前）**:
```python
self._caption_open = True
self._emit_caption("".join(self._caption_buf), True)
```
（capture でも字幕は出す＝受信側が喋るので「今喋ってる内容」。monitor と同じく capture 非依存）

**`_finish_stream`（monitor end emit と並べて）**:
```python
if self._caption_open:
    self._caption_open = False
    self._emit_caption("", False)   # 消去
self._caption_buf = []
```

エッジ:
- cancel / error / disconnect → いずれも `_finish_stream` 経由で消去。
- 空テキスト stream（AudioBegin 無しで AudioEnd）→ `_caption_open` False のまま＝何も出さない（monitor と同じ）。
- 二重 AudioBegin は `_playback_id is None` ガードで弾かれ side-band（字幕 begin は1回）。

### B. `src/text_to_shaberi/client/caption_server.py`（新規）

`AudioMonitorServer` を下敷きに、purpose は字幕。

```python
class _Subscriber:
    __slots__ = ("conn", "pending", "event", "task")
    # pending: dict | None  ← latest-wins（1件のみ）
```

```python
class CaptionServer:
    def __init__(self, *, host="127.0.0.1", port=8772, token="",
                 template: str = "", on_log=None, loop=None,
                 max_size=4096): ...
    # _cur: dict | None  ← 現在の caption 状態（mid-join 補完用）
    # _subs: dict[conn, _Subscriber]
```

- `broadcast(event, payload)`（loop 同期・await 無し）:
  - `self._cur = payload`（visible=False のときも保持＝最後の clear を再送可。あるいは visible False は `_cur=None`。下記参照）
  - 全 `_subs` に対し `sub.pending = payload; sub.event.set()`（**上書き＝coalesce**）。
- `_sender(sub)`: `while True: await event; snap=pending; pending=None; event.clear(); await conn.send(json.dumps(snap))`。例外 → `_drop_subscriber`。
- `_handle(conn)`:
  - `_authorized`（token: query `?token=` か `Authorization: Bearer`。OBS Browser Source は query しか付けられないので **query を主**とする）。
  - 登録 → mid-join: `if self._cur and self._cur.get("visible"): sub.pending = self._cur; sub.event.set()`。
  - `async for _ in conn: pass`（受信専用）。finally → `_drop_subscriber`。
- `_process_request(connection, request)`:
  - `if request.headers.get("Upgrade","").lower() == "websocket": return None`
  - else: `return Response(200, "OK", Headers([("Content-Type","text/html; charset=utf-8"), ("Cache-Control","no-store")]), self._page_bytes())`
- `_page_bytes()`: テンプレート読み込み（`template` 指定があればそのファイル、無ければ同梱 `caption/index.html`）。`importlib.resources.files("text_to_shaberi.client").joinpath("caption/index.html")` を主、`Path(__file__).parent/"caption"/"index.html"` をフォールバック。読み込み失敗時は最小インライン HTML。
- `start()` / `close()`: monitor と同型（非loopback+token必須チェック、serve、cancel+gather）。

`_drop_subscriber` は monitor と同様 idempotent（pop→cancel(自タスク除く)→ensure_future(close)）。

### C. `src/text_to_shaberi/client/caption/index.html`（新規・同梱デフォルト）

自己完結 HTML（CSS + JS インライン）。冒頭に **編集ガイドのコメント**と **CSS 変数**を置く:

```html
<!--
  Text-to-shaberi 配信字幕オーバーレイ。
  OBS の「ブラウザソース」に http://127.0.0.1:8772/ を指定（幅/高さは任意、背景=透過）。
  見た目はこのファイルだけで変えられます。フォント=@font-face かシステムフォント、
  縁取り=-webkit-text-stroke / text-shadow（多重で太く）。
-->
<style>
  :root {
    --font: "Noto Sans JP", sans-serif;
    --size: 64px;
    --color: #ffffff;
    --stroke-color: #000000;
    --stroke-width: 6px;          /* 縁取りの太さ */
    --shadow: 0 0 8px rgba(0,0,0,.8);
    --align: center;              /* center / left / right */
    --valign: flex-end;           /* 下寄せ＝flex-end / 中央=center */
    --fade-ms: 180ms;
    --max-width: 90vw;
  }
  html,body { margin:0; height:100%; background: transparent; overflow:hidden; }
  #wrap { display:flex; height:100%; align-items:var(--valign); justify-content:var(--align); }
  #cap {
    font-family: var(--font); font-size: var(--size); color: var(--color);
    -webkit-text-stroke: var(--stroke-width) var(--stroke-color);
    paint-order: stroke fill;     /* 塗りの上に線を載せない＝可読性 */
    text-shadow: var(--shadow);
    max-width: var(--max-width); line-height:1.3; padding: .2em .4em;
    opacity:0; transition: opacity var(--fade-ms) ease; white-space: pre-wrap; word-break: break-word;
  }
  #cap.show { opacity:1; }
</style>
<div id="wrap"><div id="cap"></div></div>
<script>
  const cap = document.getElementById('cap');
  let lastSeq = -1;
  function apply(m){
    if (typeof m.seq === 'number' && m.seq < lastSeq) return; // 順序逆転を捨てる
    lastSeq = m.seq ?? lastSeq;
    if (m.visible) { cap.textContent = m.text || ""; cap.classList.add('show'); }
    else { cap.classList.remove('show'); }
  }
  function connect(){
    const url = (location.protocol==='https:'?'wss://':'ws://') + location.host + '/ws' + location.search;
    const ws = new WebSocket(url);
    ws.onmessage = e => { try { apply(JSON.parse(e.data)); } catch(_){} };
    ws.onclose = () => setTimeout(connect, 1000); // クライアント再起動時に自動復帰
  }
  connect();
</script>
```

> `textContent` で注入（HTML エスケープ）。`location.search` を WS にも引き継ぐ＝非loopback時の `?token=` をそのまま渡せる。

### D. `src/text_to_shaberi/client/config.py`

`MonitorConfig` と並べて:
```python
@dataclass(slots=True)
class CaptionConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8772
    token: str = ""
    template: str = ""   # ユーザーHTML差し替えパス（空=同梱デフォルト）
```
- `ClientConfig.caption: CaptionConfig = field(default_factory=CaptionConfig)`。
- `load_client_config`: `caption=CaptionConfig(**_only(raw.get("caption") or {}, CaptionConfig))`。
- 検証: monitor と同型 — 非loopback host かつ token 未設定 → `enabled=False` にしてログ。

### E. `src/text_to_shaberi/client/app.py`

monitor の配線に並走させる:
- `from .caption_server import CaptionServer`
- `_caption_server_holder = [None]`
- `def _on_caption(event, payload): srv = _caption_server_holder[0]; if srv: srv.broadcast(event, payload)`
- `WSClient(..., on_caption=_on_caption)`
- `if cfg.caption.enabled: caption_server = CaptionServer(host=..., port=..., token=..., template=..., on_log=window.log, loop=loop); _caption_server_holder[0] = caption_server`
- `_startup`: `await caption_server.start()`（try/except でログ）。成功時 `window.log(f"[caption] 字幕URL: http://{host}:{port}/ （OBSブラウザソースに貼る）")`。
- `_shutdown`: `await caption_server.close()`。

### F. ドキュメント・設定例

- `docs/USECASES.md`: ④「配信字幕」節を追加（OBS ブラウザソース手順、URL、装飾の変え方、透過の注意）。
- `docs/PROTOCOL.md`: 「字幕 WS イベント（Python→ブラウザ）」節（`caption` イベントスキーマ）＝他者が独自オーバーレイを作れる仕様。
- `client.toml` は **gitignored**（ユーザー個人設定）。コミットしない。例として `[caption] enabled / host / port / token / template` を docs に明記（実トークン非記載）。
- `docs/HANDOVER.md`: §7 に「text-stream はサーバが per-segment 音声境界を返さない」を追記。§2 に「字幕は latest-wins mailbox（snapshot 冪等）」を追記。

## テスト計画

| ファイル | 内容 |
|---|---|
| `tests/test_ws_caption.py`（新規） | tee 相関を `_on_message`/`send_text_segment` 直接駆動で検証。`test_ws_monitor_tee.py` の module-level `_LOOP` パターン踏襲。<br>① speak: AudioBegin で `caption{text, visible:true}`、AudioEnd で `{visible:false}`。<br>② text-stream 蓄積: seg0送信→AudioBegin→show "seg0"、seg1送信→update "seg0seg1"、AudioEnd→clear。<br>③ STT: Transcript→AudioBegin→show transcript→AudioEnd→clear。<br>④ capture でも caption 発火。<br>⑤ cancel/disconnect→`_finish_stream`→clear。<br>⑥ on_caption=None で無害（emit しない）。<br>⑦ emit 例外が recv/再生へ漏れない。 |
| `tests/test_caption_server.py`（新規） | ① broadcast→購読者が caption 受信。② coalesce（遅い購読者は最新のみ）。③ mid-join（visible 中の接続に現在状態送信）。④ HTTP GET→ページ本文（`process_request` が Response 返す）。⑤ WS upgrade は process_request が None。⑥ 切断/送信失敗で drop（idempotent）。⑦ 非loopback+token無→start で RuntimeError。⑧ token 必須時の query 認証可否。 |
| `tests/test_client_config.py`（追記） | `[caption]` デフォルト・指定値・非loopback+token無→enabled False・template 反映。 |

- 既存 engine/sink ダブルは **無改変**（`on_caption` は optional・contract 非変更）。前回の 30 件破壊は起きない。
- offscreen smoke: fake server + offscreen GUI で caption 有効化 → 既知良の WS リスナーを 8772/ws に繋ぎ、speak→`caption{visible:true}`→`{visible:false}` を受信（`CAPTION_OK`）。HTTP GET でページ取得も確認。

## セキュリティ / 不変条件

- 既定 127.0.0.1 bind。非loopback は token 必須（monitor と同条件）。
- 外部送信なし・静的ローカルページのみ。フォントはローカル（@font-face かシステム）。
- 喋り内容は `textContent`（HTML/スクリプト注入防止）。
- VAD はクライアント側のまま（本機能は関与しない）。
- コミットは branch→ff-only main、push は明示依頼時のみ。`server.docker.toml`/`.env`/`assets/*.wav`/`ToDo.md` 不コミット。

## 進め方（ワークフロー）

1. 本計画書 → **codex レビュー**（read-only, `< /dev/null`）→ 指摘反映。
2. 実装（A〜F）。
3. pytest（新規＋既存緑）＋ ruff（自分の変更分クリーン）＋ offscreen smoke。
4. branch にコミット → ff-only で main（push しない）。
5. ハマりは HANDOVER.md + GaOTTT へ。

## codex への観点（レビュー依頼で強調する点）

1. **text-stream 蓄積の競合**: `_caption_buf` の append（send_text_segment）と read（AudioBegin）が同一ループで安全か。AudioBegin 前に複数 seg がバッファされるケース、AudioBegin が seg 到着前に来るケース。
2. **CaptionServer の latest-wins mailbox**: coalesce で「clear が show に上書きされ取りこぼす」ことが無いか（seq 単調＝最後の状態が必ず勝つ設計か）。mid-join の `_cur` と pending の二重送出で順序が壊れないか。
3. **process_request の WS 判定**: `Upgrade: websocket` 判定で取りこぼし（大文字小文字・複数値）が無いか。非WS GET に Response を返して正しく HTTP 終了するか。
4. **drop の idempotent**: stall は無い（mailbox）が、send 失敗/切断/close の三経路で二重 drop しないか。
5. **_finish_stream の clear 一回性**: 全終了経路（AudioEnd/Stopped/StreamStopped/Error/cancel/disconnect）で clear がちょうど1回、かつ begin 未発火なら出さないか（monitor `_monitor_open` と同パターン）。

---

## codex レビュー反映（判定: GO-with-fixes → 実装は v2 で行う）

レビュー全文: `docs/REVIEW-codex-CAPTION.md`。以下8点を実装に織り込む。

### NO-GO（必須修正）

1. **mid-join での clear 取りこぼし**: `_cur` は `visible:false` も保持し、新規購読者へ**常に**送る（初期状態のみ `_cur=None`）。
   → `broadcast` は subs ゼロでも `_cur` を必ず更新。`_handle` の mid-join は `if self._cur is not None: sub.pending = self._cur; sub.event.set()`（visible 問わず）。
   切断中に発話が終わっても、再接続した購読者は最後の clear を受けて確実に隠れる。

2. **`seq` のプロセス再起動巻き戻り**: ブラウザは **WS `onopen` ごとに `lastSeq = -1` リセット**。
   → アプリ再起動は必ず socket 切断→再接続（onclose→onopen）を伴うので、onopen リセットだけで完全に解消（session_id は不要）。
   index.html の `ws.onopen = () => { lastSeq = -1; }` を追加。

3. **text-stream の await 越し競合（終了後 append→次発話混入）**: `send_text_segment` を「送信成功後、同一 stream 生存時のみ append」に。
   ```python
   async def send_text_segment(self, index, text):
       async with self._send_lock:
           sid = self._active_id
           if sid is None or not self._stream_mode:
               return
           await self._ws_send(SegText(id=sid, index=index, text=text).model_dump_json())
           # 送信中に _finish_stream が走った場合は append しない（次発話への混入防止）
           if self._active_id == sid and self._stream_mode:
               self._caption_buf.append(text)
               if len(joined := "".join(self._caption_buf)) > self._caption_max:
                   self._caption_buf[:] = [joined[-self._caption_max:]]  # 末尾優先で折りたたみ（MED8）
               if self._caption_open:
                   self._emit_caption("".join(self._caption_buf), True)
   ```
   append は**送信前に行わない**。`start_text_stream`/`start_stream` で buf リセットは維持。

### MED（実装に含める）

4. **wire envelope**: 字幕イベントは `broadcast` 側で封筒を作る。
   ```python
   def broadcast(self, event, payload):
       snap = {"event": event, **payload}   # {"event":"caption","text":..,"visible":..,"seq":..}
       self._cur = snap
       for sub in list(self._subs.values()):
           sub.pending = snap; sub.event.set()   # coalesce（最新が必ず勝つ）
   ```
   sender は `snap = sub.pending; sub.pending = None; sub.event.clear()` の間に **await を挟まない**＝latest-wins 厳守。mid-join も pending 経由の一経路に統一。

5. **process_request のルーティング厳格化**:
   ```python
   path = urlsplit(request.path).path
   is_ws = any(v.strip().lower() == "websocket" for v in request.headers.get("Upgrade","").split(","))
   if is_ws:
       if path != "/ws": return self._resp(404, b"not found")
       if not self._origin_ok(request): return self._resp(403, b"forbidden origin")
       if not self._authorized(request): return self._resp(401, b"unauthorized")
       return None  # WS ハンドシェイクへ
   if path in ("/", "/index.html", "/caption", "/caption.html"):
       return self._resp(200, self._page_bytes(), content_type="text/html; charset=utf-8")
   return self._resp(404, b"not found")
   ```
   WS は **`/ws` のみ**。`Upgrade` はカンマ分割で複数値耐性。`Response(status, reason, Headers([...Content-Type, Content-Length...]), body)`。

6. **終了一回性テストのパラメータ化**: AudioEnd / Stopped / StreamStopped / Error / cancel応答 / disconnect / 送信失敗 / format不正 / 空stream を網羅し、
   「begin 最大1回・clear ちょうど1回・begin無しなら clear無し」を assert。

7. **Origin 制限 + 定数時間トークン比較**:
   - `_authorized`: `hmac.compare_digest` で qs_token / bearer を比較。
   - `_origin_ok`: `Origin` 無し（非ブラウザツール）は許可。present なら netloc host が `{host,127.0.0.1,localhost,::1}` のいずれかのみ許可（第三者Webページの CSWSH を遮断）。
   - 字幕は「これから配信に出る内容」なので機微度は低いが、安価な多層防御として入れる。

8. **字幕全文の上限**: `CaptionConfig.max_chars`（既定 200）。蓄積時に超過したら **末尾優先で切り詰め**（今喋ってる直近が残る）。buf も `[joined[-max:]]` に折りたたんでメモリ上限化。

### v2 で変わる config

```python
@dataclass(slots=True)
class CaptionConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8772
    token: str = ""
    template: str = ""
    max_chars: int = 200   # 蓄積字幕の最大長（末尾優先で切詰め）
```
WSClient へ `caption_max` を渡す（`__init__(..., caption_max: int = 200)`）。
