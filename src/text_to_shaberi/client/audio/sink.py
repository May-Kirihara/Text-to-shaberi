"""受信(asyncio)→再生(thread) の橋渡し（PlaybackSink）。

1発話 = 1 sink ライフサイクル（begin → feed×N → end / abort）。
feed/end/abort は sentinel 投入のみで即 return（非ブロッキング契約）。
worker は daemon スレッドで自走し drain/close する。
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from .player import StreamingPlayer

_END = object()
_ABORT = object()


class PlaybackSink:
    def __init__(
        self,
        *,
        player_factory: Callable[..., Any] = StreamingPlayer,
        on_log: Callable[[str], None] | None = None,
        loop: Any | None = None,
    ) -> None:
        self._player_factory = player_factory
        self._on_log = on_log
        self._loop = loop
        self._player: Any | None = None
        self._q: queue.Queue[Any] = queue.Queue(maxsize=256)
        self._thread: threading.Thread | None = None
        self._aborting = False

    def _log(self, msg: str) -> None:
        if self._on_log is None:
            return
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_log, msg)
        else:
            self._on_log(msg)

    def begin(self, fmt: Any, device: Any) -> None:
        self._q = queue.Queue(maxsize=256)
        self._aborting = False
        self._player = None
        self._thread = threading.Thread(target=self._run, args=(fmt, device), daemon=True)
        self._thread.start()

    def _run(self, fmt: Any, device: Any) -> None:
        try:
            player = self._player_factory(
                src_rate=fmt.sample_rate,
                channels=fmt.channels,
                device=device,
                encoding=fmt.codec,
            )
            self._player = player
        except Exception as exc:
            self._log(f"[sink] player 生成失敗: {exc}")
            return

        while True:
            item = self._q.get()
            if item is _END:
                try:
                    player.close()
                except Exception:
                    pass
                break
            if item is _ABORT:
                try:
                    player.close()
                except Exception:
                    pass
                break
            try:
                player.write_bytes(item)
            except Exception:
                self._log("[sink] write_bytes 例外")
                try:
                    player.close()
                except Exception:
                    pass
                break

    def feed(self, chunk: bytes) -> bool:
        try:
            self._q.put_nowait(chunk)
            return True
        except queue.Full:
            return False

    def end(self) -> None:
        try:
            self._q.put_nowait(_END)
        except queue.Full:
            self._q.put(_ABORT)

    def abort(self) -> None:
        if self._aborting:
            return
        self._aborting = True
        if self._player is not None:
            self._player.request_abort()
        try:
            self._q.put_nowait(_ABORT)
        except queue.Full:
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._q.put_nowait(_ABORT)

    def join(self, timeout: float = 2.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
