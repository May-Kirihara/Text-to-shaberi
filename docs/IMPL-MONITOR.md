# IMPL-MONITOR — 音声モニタ（全合成音声の broadcast / 口パクエンジン向け）

## 1. 目的 / 非目的

**目的**：クライアントが合成・再生する**全ての音声**を、ローカル WS の**専用ポート**に接続した購読クライアントへ **broadcast** する。主用途は「音声から口形を推論する口パクエンジン」。受信・解析は購読側が実装するので、こちらは**復号 PCM を発話単位で流し続ける**だけ（≒「WSに生えた仮想オーディオデバイス」）。

確定した設計方針（ユーザー合意）：
- 範囲 = **全合成音声**（GUIテキスト / マイクVC / LLM text-stream / forward_audio capture も含む）
- 接続口 = **新規の専用ポート**（既存入力口 8770 とは別。forward_audio の並行ロジックに一切触れない）
- 形 = **発話単位フレーミング**（`audio_begin` → pcm × N → `audio_end`。無音区間はデータを流さない）

**非目的**：購読側からの制御（テキスト投入・cancel 等）。サーバ直結（それは `PROTOCOL.md`）。常時無音フレーム送出（真の連続デバイス化）。viseme/OSC 等の高位プロトコル。

## 2. アーキテクチャ

```
合成音声(全経路) ──ws_client が復号──┬─→ 既存: engine 再生(OBS) / または capture tap(forward_audio)
                                      └─→ 新規: on_monitor tee ──→ AudioMonitorServer.broadcast
                                                                      └─(per-subscriber queue)→ 購読者1..N
```

- **純 broadcast（tee/コピー）**。forward_audio のような reservation / pin / done-exactly-once / cancel ルーティングは**一切不要**（購読は request と無関係）。
- ローカル再生は従来どおり継続。monitor は**コピーを配るだけ**。
- 既定 OFF・localhost 限定。**サーバ側変更なし**（`ready.features` 不要：純クライアント機能）。

## 3. ws_client の変更（monitor tee）

`WSClient.__init__` に `on_monitor: Callable[[str, Any], None] | None = None` を追加し `self._on_monitor` 保持、`self._monitor_open = False` 追加。

**例外隔離（codex MED3）**：emit は必ず `_emit_monitor` 経由。recv loop 上で呼ぶため、callback/broadcast の例外が再生・受信ループへ漏れると障害になる。best-effort tee として握る：
```python
def _emit_monitor(self, kind, payload):
    if self._on_monitor is None:
        return
    try:
        self._on_monitor(kind, payload)
    except Exception:
        self._log("monitor emit 失敗")  # best-effort: 再生/recv へは漏らさない
```

emit は**全ストリーム共通**（capture か通常かを問わない）に3点：

1. **AudioBegin**（有効 active 判定＋format 検証を通過し、`decoded_sr/decoded_ch` 確定後、`is_capture` 分岐の**前**）：
   ```python
   self._monitor_open = True
   self._emit_monitor("begin", {"codec": "pcm_s16le", "sample_rate": decoded_sr, "channels": decoded_ch})
   ```
   ※ format 不正/side-band は手前で return 済み → 正常 active のみ begin。`_monitor_open=True` は `_on_monitor` の有無に関わらず立てる（無ければ emit が no-op なだけで、end 整合は保たれる）。

2. **_on_binary**（Opus 復号 or 生 PCM で `pcm` 確定後、`is_capture` 分岐の**前**）：
   ```python
   self._emit_monitor("chunk", pcm)
   ```
   ※ `_on_binary` 冒頭の `not active/not playback_id/cancelling` ガードを通った後なので、chunk は begin と整合。decode 失敗時は手前で return → chunk は出ない。

3. **_finish_stream**（全終了経路の単一終端。capture tap と同型で先頭に）：
   ```python
   if self._monitor_open:
       self._monitor_open = False
       self._emit_monitor("end", None)
   ```
   ※ 音声ゼロ完了（空テキスト等）は begin 未発火＝`_monitor_open` False → 誤 end なし。cancel/エラー終了でも end は1回。

> capture stream では `on_audio`(request conn へ pin)と `on_monitor`(broadcast)が**両方**発火＝両者が音声を得る（仕様どおり）。monitor は capture の再生抑制とは独立。

## 4. AudioMonitorServer（新規 `client/audio_monitor.py`）

`LocalInputServer` の outbound 機構（deque + event + sender、chunk のみ drop）を**購読者ごと**に複製した broadcast 専用サーバ。

```python
class _Subscriber:           # __slots__: conn, queue(deque), event, task, drop_warned
    ...

class AudioMonitorServer:
    def __init__(self, *, host="127.0.0.1", port=8771, token="",
                 on_log=None, loop=None, queue_max=256, max_size=65536): ...
    async def start(self): ...      # websockets.serve(self._handle, host, port)
    async def close(self): ...      # _closing=True; 全 sub.task cancel; server close
    def broadcast(self, kind, payload): ...   # loop スレッドから同期呼び出し（tee）
```

**broadcast(kind, payload)**（同期・await 無し＝iteration 中に他コルーチンが割り込まない）：
- `if self._closing or not self._subs: return`
- 現フォーマット追跡：`kind=="begin"` で `self._cur_format = payload`、`kind=="end"` で `self._cur_format = None`。
- `for sub in list(self._subs.values()): self._enqueue(sub, kind, payload)`（`list()` で防御的コピー）。

**_enqueue(sub, kind, payload)**（chunk-only-drop ＋ stall 購読者は切断＝codex MED2「framing 保証 or 接続終了」）：
- queue に空きがあれば append。
- 満杯なら deque 内の最初の `chunk` を1つ退避して空ける。
  - **退避できた** → append（begin/chunk/end いずれも入る）。
  - **退避できない（queue が制御イベントだけで埋まっている＝sender が全く drain していない死/停滞購読者）** → `self._drop_subscriber(sub)` して**この購読者を切断**し return。← begin/end を無制限に積んで購読者単位メモリリークさせない。
- これで per-subscriber メモリは `queue_max` で有界、生存購読者には framing 保証。

**_drop_subscriber(sub)**（idempotent。stall / send 失敗 / 切断 の全経路で共用＝codex MED1）：
```python
def _drop_subscriber(self, sub):
    if self._subs.pop(sub.conn, None) is None:
        return  # 既に除去済み
    t = sub.task
    if t is not None and t is not asyncio.current_task():
        t.cancel()                       # 自タスクからの自己 cancel は避ける
    asyncio.ensure_future(self._safe_close(sub.conn))  # best-effort close
```

**_sender(sub)**：deque を drain して `sub.conn.send` で送出（begin→`{"event":"audio_begin","format":payload}`、chunk→binary、end→`{"event":"audio_end"}`）。送信例外時は `finally` で `self._drop_subscriber(sub)`（_handle の `async for` 終了に依存しない・codex MED1）。

**_handle(conn)**（購読接続）：
- `_authorized` 不可 → 4401 close。`_closing` → 4409 close。
- `_Subscriber` 生成、`sub.task = ensure_future(_sender(sub))`、`self._subs[conn]=sub`。
- **mid-stream join**：`if self._cur_format is not None: self._enqueue(sub, "begin", self._cur_format)`（発話途中参加でも先頭に begin を補い「chunk が begin 前に来る」破綻を防ぐ）。
- `async for _ in conn: pass`（購読者入力は無視＝受信専用）。
- finally：`self._drop_subscriber(sub)`（idempotent）。

**並行安全（codex 観点の先取り）**：
- broadcast は loop スレッドのみ・await 無し → deque 変更/iteration は他コルーチンと競合しない。sub の add/remove は await 点でのみ起き、broadcast の同期ループ中には起きない（`list()` でさらに防御）。
- 遅い/死んだ購読者：chunk-drop で queue 成長を抑え、完全停滞は切断。send 失敗で sender 終了→`finally` が `_drop_subscriber`。**合成・他購読者を一切ブロックしない**。
- close と sender/broadcast の競合：`_closing` で broadcast 早期 return。`close()` は全 `sub.task` を cancel し `await asyncio.gather(*tasks, return_exceptions=True)`（未回収 task warning 回避・codex LOW）。

## 5. config

`MonitorConfig`（`config.py`）：
```python
@dataclass(slots=True)
class MonitorConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8771
    token: str = ""
    queue_max: int = 256
```
`ClientConfig.monitor` 追加、`load_client_config` で `monitor=MonitorConfig(**_only(raw.get("monitor") or {}, MonitorConfig))`。input と同じく**非loopback + token 未設定なら enabled=False** に落とす検証を追加。

## 6. app 配線

- `_monitor_server_holder: list = [None]` と
  ```python
  def _on_monitor(kind, payload):
      srv = _monitor_server_holder[0]
      if srv is not None:
          srv.broadcast(kind, payload)
  ```
- `WSClient(..., on_monitor=_on_monitor)`。
- `if cfg.monitor.enabled:` で `AudioMonitorServer(host/port/token/queue_max, on_log, loop)` を生成し `_monitor_server_holder[0]=...`。
- `_startup`：`await monitor_server.start()`（try/except でログ）。
- `_shutdown`：`await monitor_server.close()`。
- `client.toml` サンプルに `[monitor]` 節を追記、`run_check`/doctor 系があれば `[monitor]` の enabled/port を表示（codex LOW）。

## 7. 購読側ワイヤ仕様（USECASES.md に記載）

接続：`ws://127.0.0.1:8771`（token 設定時は `?token=...` か `Authorization: Bearer`）。送信不要。受信のみ：
```
← {"event":"audio_begin","format":{"codec":"pcm_s16le","sample_rate":N,"channels":C}}
← (binary) pcm_s16le フレーム × N
← {"event":"audio_end"}
（次の発話まで何も来ない。発話ごとに begin..end を繰り返す）
```
- `sample_rate` は Opus 経路 48000 / PCM 経路プロファイル依存。固定したいなら `[server].codec="pcm"`。
- 遅延すると chunk が間引かれる（口パクは振幅ベースなので実用上問題なし）。begin/end は保証。
- 複数購読可。途中参加は次（or 進行中なら補完）の begin から整合。

## 8. テスト

- `tests/test_audio_monitor.py`：
  - broadcast が複数購読者へ begin/chunk/end を順序どおり配信
  - chunk-drop（queue_max 超過で chunk のみ間引き、begin/end は不可侵）
  - **stall 購読者の切断**（drain しない購読者へ制御イベントが queue_max を超えたら `_drop_subscriber` され、他購読者は無傷・codex MED2）
  - 購読者切断でクリーンアップ・残り購読者へ影響なし（`_drop_subscriber` idempotent・codex MED1）
  - 購読者ゼロで broadcast が no-op
  - mid-stream join で begin が補完される
  - close で全 sender 停止（`gather` await）
- `tests/test_ws_textstream.py` 等に ws_client tee：通常(非capture)ストリームで on_monitor が begin/chunk/end を1回ずつ、capture でも発火、空テキストで begin/end が出ない、**二重 begin は side-band 扱いで monitor へ begin→begin にならない（既存 AudioBegin の active 判定に従う／未保証なら明記）**（codex LOW）、**on_monitor が例外を投げても再生/recv が継続する**（`_emit_monitor` の握り・codex MED3）、を FakeMonitor で検証。

## 9. ドキュメント

- `USECASES.md` ③ に「音声モニタ（口パク等へ配信）」小節（§7 のワイヤ仕様 + `[monitor]` 設定例）。
- `HANDOVER.md` に1行（monitor tee は全ストリーム発火・broadcast は同期 await 無しで安全）。

## 10. 工程

ブレスト(済) → 本計画書 → **codex レビュー(計画)** → 実装 → **codex レビュー(実装・自作並行コードなので必須)** → pytest/ruff/offscreen smoke → commit/PR。
