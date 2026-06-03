# アーキテクチャ改訂 v0.3: クライアント–サーバ分離

> 本書は `docs/PLAN.md` v0.2（モノリシック前提）を**改訂**する。ヒアリング(2026-06-03)で
> 「フロント=Windows(マイク)／処理=ヘッドレスGPUサーバ」という分離構成が確定したため。
> フロー: 本書を codex レビュー → 反映 → 各マイルストーンの実装計画 → GLM 委託 → 検証。

## 1. 変更点（モノリシック → 分離）
- 旧: 1プロセスで mic→STT→TTS→再生（GPUマシン上）。
- 新: **Windowsクライアント（GUI/マイク/VAD/再生）** ↔ **WebSocket** ↔ **ヘッドレスGPUサーバ（STT/TTS）**。
- 既存実装(M0足場/M1 TTSコア/M2 StreamingPlayer/IME)は**捨てず配置換えして再利用**（§8 移行マップ）。

## 2. 全体アーキテクチャ
```
 Windows クライアント                              Linux GPU サーバ (headless)
┌───────────────────────────┐   WebSocket   ┌──────────────────────────────┐
│ PySide6 GUI                │◄─ws(s)://────►│ WS backend (FastAPI/websockets)│
│ マイク取得 + VAD(Silero)    │  制御=JSON     │  ├ Orchestrator(セッション/割込) │
│ テキスト入力 / API入力(LLM) │  音声=binary   │  ├ STT: Qwen3-ASR (GPU常駐)      │
│ StreamingPlayer 再生        │  (PCM/Opus自動)│  ├ TTS: GPT-SoVITSクライアント   │
│ 出力デバイス選択(VB-CABLE等) │                │  │      (tomlプロファイル)        │
└───────────────────────────┘                │  └ Codec(PCM/Opus) + 認証/TLS   │
                                              └───────────────┬──────────────┘
                                                GPT-SoVITS :9880 (別プロセス・既存)
```

## 3. リポジトリ / 依存の再編
```
src/text_to_shaberi/
├── common/    # 共有: protocol(WSメッセージ&スキーマ) / audio_format / codec(PCM,Opus) / 設定共通
├── server/    # バックエンド: ws_server / orchestrator / stt(Qwen) / tts(GPT-SoVITSクライアント+profile)
├── client/    # フロント: ui(PySide6) / mic(sounddevice+VAD) / player(StreamingPlayer) / devices / ws_client
└── (entry)    # text-to-shaberi-server  /  text-to-shaberi-client
```
- **依存グループ**（`pyproject` optional-dependencies）:
  - `server`: torch(cu128), qwen-asr, fastapi, uvicorn, websockets, httpx, numpy, (soxr)
  - `client`: PySide6, qasync, sounddevice, soxr, numpy, websockets, (opus: `opuslib`/`PyOgg`)
  - `common`: numpy, pydantic, tomllib(標準)
- **Windowsクライアントは client+common のみインストール**（torch/qwen 不要）。サーバは server+common。
- 設定: `server.toml` / `client.toml` に分割（共通ローダ）。TTSプロファイルはサーバ側 `profiles/tts/*.toml`。
- **クライアントUI方針**: メイン画面は**シンプル維持**（入力・送信・停止・出力先・ログ）。増えていく設定は**歯車(設定)ボタン → 設定ダイアログ**に集約する（ハンバーガーでも可）。設定ダイアログの項目: **サーバWSアドレス / 認証トークン / 接続(LAN・遠隔) / コーデック(自動/PCM/Opus) / 出力デバイス / TTSプロファイル選択 / 将来項目**。設定は `client.toml` に永続化し、起動時ロード・変更時保存。機能トグル（例: 自動TTS, マイクON/OFF）も同ダイアログに。

## 4. WebSocket プロトコル（v1）
- 接続: `ws(s)://host:port/ws`。認証は接続時 `Authorization: Bearer <token>`（または最初の `hello` に同梱）。
- 制御は**テキスト(JSON)フレーム**、音声は**バイナリフレーム**。M-scopeは方向ごとに**同時1ストリーム**前提（多重化はキュー導入時に拡張）。
- ハンドシェイク:
  - C→S `{"type":"hello","caps":{"codecs":["pcm_s16le","opus"]}}`
  - S→C `{"type":"ready","codec":"pcm_s16le","tts":{"sample_rate":32000,"channels":1}}`（環境/設定でcodec決定）
- 発話(テキスト→音声):
  - C→S `{"type":"speak","id":"u1","text":"…","profile":"gpt-sovits","interrupt":false}`
  - S→C `{"type":"audio_begin","id":"u1","format":{"codec":"pcm_s16le","sample_rate":32000,"channels":1}}`
  - S→C `<binary audio chunk> …`（直近 audio_begin に属する）
  - S→C `{"type":"audio_end","id":"u1"}`
- 音声認識(マイク→テキスト):
  - C→S `{"type":"stt_begin","id":"u2","format":{"codec":"pcm_s16le","sample_rate":16000,"channels":1}}`
  - C→S `<binary audio chunk> …`（発話セグメント）
  - C→S `{"type":"stt_end","id":"u2"}`
  - S→C `{"type":"transcript","id":"u2","text":"…","final":true}`
  - サーバ設定で **自動TTS** なら続けて audio_begin/…/audio_end も送る。
- 制御/エラー:
  - C→S `{"type":"stop","id":"u1"}` → サーバはTTS/STT生成をキャンセル、`{"type":"stopped","id":"u1"}`。
  - S→C `{"type":"error","id":…,"message":"…"}`
- スキーマは `common/protocol.py`（pydantic）で型定義し client/server で共有。

## 5. 音声コーデック戦略（自動 PCM/Opus）
- `common/codec.py`: `encode(frames,fmt)->bytes` / `decode(bytes,fmt)->frames`。`pcm_s16le`(無変換) と `opus`(48k/20msフレーム) を実装。
- ネゴシエーション: サーバ設定 `transport="auto"|"pcm"|"opus"`。`auto` は接続元が loopback/LAN(プライベートIP) なら PCM、それ以外 Opus。`hello.caps` と突き合わせて `ready.codec` 決定。
- TTS出力(32kHz)→Opus(48k)変換は soxr。受信側で逆変換。**rawは AudioFormat を audio_begin で明示**（§PLAN §8 の原則を踏襲）。

## 6. セキュリティ
- LAN: `token` 任意（既定は共有トークン1個）。遠隔: **token必須 ＋ wss/TLS必須**。
- サーバは既定 `0.0.0.0` ではなく設定で bind を明示。CORS/Origin チェック。レート制限は後続。

## 7. レイテンシ予算（ネットワーク込みで再評価）
- LAN: WS往復 ~1–5ms。**GUI/API TTFA = 送信 + net + サーバ(GPT-SoVITS first-chunk 148–270ms 実測) + net + 復号/再生** ≈ ローカル+5–15ms → **目標 p50≤400ms 維持可**。
- マイク往復 = mic+VAD(端末) + net + STT(0.29s) + TTS first-chunk + net + 再生 ≈ LANで+10–20ms。
- Opus enc/dec ~数ms。遠隔は RTT 次第（受け入れ基準は LAN を主、遠隔は best-effort）。

## 8. 既存コードの移行マップ（M0–M2 の配置換え）
| 既存 | 移行先 | 備考 |
|---|---|---|
| `tts/client.py`(TTSClient/stream/decode) | `server/tts/` | サーバ内でGPT-SoVITSをstreaming |
| `tts/profile.py` | `server/tts/`（一部fmtは`common`） | tomlプロファイル |
| `audio/player.py`(StreamingPlayer) | `client/audio/` | **lazy-start修正込み**で再利用 |
| `audio/devices.py` | `client/audio/` | Windowsは VB-CABLE をデバイス選択 |
| `audio/routing.py`(PipeWire) | `client/audio/`(Linux/dev用) | Windows本番では未使用 |
| `ui/main_window.py` | `client/ui/` | WS送信に結線 |
| `api/server.py`(FastAPI) | `server/ws_server` | WSバックエンドへ昇格(REST /speak も残置可) |
| `config.py` | `common`＋server/client個別 | server.toml/client.toml |
| `app.py` | `client`(run_gui)＋`server`(run_server) | エントリ2つ |

## 9. 改訂マイルストーン
- **R0 再編**: common/server/client へ分割、依存グループ、`common/protocol.py`(pydantic)、既存コード移設、両エントリの空起動。
- **S1 サーバTTS-WS**: `speak`→GPT-SoVITS streaming→PCM音声フレーム送信＋`stop`キャンセル。
- **C1 クライアント再生**: GUI→WS `speak`→受信→**StreamingPlayer(lazy-start修正)** 再生。LAN/PCMで「テキスト→声」疎通＋TTFA計測。＋**歯車ボタンの設定ダイアログ最小版**（サーバWSアドレス/トークンを設定→`client.toml`保存、再接続）。
- **S2 サーバSTT-WS**: 音声受信→Qwen→`transcript`（＋自動TTSオプション）。
- **C2 マイク+VAD**: 端末マイク常時録音・Silero VAD・発話セグメント→WS→transcript/音声往復（マイク往復完成）。
- **X1 コーデック/セキュリティ**: PCM/Opus自動＋token認証＋wss/TLS（遠隔対応）。
- **X2 仕上げ**: ネット込みレイテンシ計測・WS再接続/バックプレッシャ・設定UI・Windows実機(VB-CABLE)・エラー処理。

## 10. リスク・未決
1. **Windows音声(WASAPI)** での sounddevice/StreamingPlayer 挙動（latency, lazy-start, デバイス列挙, VB-CABLE）→ 実機検証。
2. **Opusの依存**（`opuslib`/`PyOgg` のWindowsホイール入手性）→ 無ければ PCM 限定で先行。
3. **WSバイナリのid関連付け**（M-scopeは方向ごと単一ストリーム前提。多重化はキュー導入で）。
4. **Silero VAD のWindows動作**（torch依存の有無。webrtcvadフォールバック検討）。
5. **再接続/切断時**のセッション復帰・バックプレッシャ（送信過多で詰まらせない）。
6. STT結果→自動TTSの**サーバ内連結**か、クライアント往復かのフロー確定（設定で選べる形を推奨）。

## 11. 受け入れ基準（疎通MVP）
- [ ] サーバ(headless)起動＋クライアント(GUI)が ws 接続・`hello/ready` 成立。
- [ ] テキスト送信→サーバTTS→クライアント再生（**先頭欠け無し**）。**TTFA(ネット込み, LAN) ≤ 400ms** をログ表示。
- [ ] マイク発話→サーバSTT→transcript表示→（自動TTSなら）再生。
- [ ] `stop` で音停止＋サーバ生成キャンセル。
- [ ] LAN/PCM と Opus 双方で疎通。token認証（遠隔は wss）。
- [ ] **設定ダイアログ(歯車)** からサーバWSアドレス/トークン等を変更・`client.toml`保存でき、再接続に反映。
- [ ] サーバ/クライアント各失敗（接続断・サーバ不達・OOM）でクラッシュせず復帰。

## 12. PLAN.md との関係
- §3 SLO・§7 tomlプロファイル・§5 STT・§8 音声I/O原則は**維持**（配置がサーバ/クライアントに分かれるだけ）。
- §6 並行設計はクライアント(qasync+mic/再生スレッド)とサーバ(asyncio WS + STTワーカースレッド)に分割。
- 旧 M3–M6 は本書 §9 の S1/C1/S2/C2/X1/X2 に再編。
