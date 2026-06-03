"""持続的プレイバックエンジン（X3: 出力ストリーム常時開放・ウォームアップ1回償却）。

1本の OutputStream ＋ 1本の常駐 worker を複数発話で再利用。
アイドル時は無音 keepalive で device を warm 維持し、ウォームアップを1回に償却。
seq 方式の単一所有 worker で状態 race を排除。協調 abort で stream を閉じず warm 維持。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from queue import Full
from typing import Any, Callable

import numpy as np

from ...common.pcm import _pcm_to_float32, bytes_per_sample, split_full_samples
from .player import _device_samplerate


@dataclass(slots=True)
class _Begin:
    seq: int
    fmt: Any
    device: Any


@dataclass(slots=True)
class _Audio:
    seq: int
    data: bytes


@dataclass(slots=True)
class _End:
    seq: int


_ABORT = object()
_CLOSE = object()


class PlaybackEngine:
    def __init__(
        self,
        *,
        on_log: Callable[[str], None] | None = None,
        loop: Any | None = None,
        keepalive_ms: int = 20,
        warm_min_ms: int = 300,
        max_write_ms: int = 20,
        queue_max: int = 512,
        stream_factory: Callable[..., Any] | None = None,
        resampler_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._on_log = on_log
        self._loop = loop
        self._keepalive_ms = keepalive_ms
        self._warm_min_ms = warm_min_ms
        self._max_write_ms = max_write_ms
        self._queue_max = queue_max
        self._stream_factory = stream_factory
        self._resampler_factory = resampler_factory

        self._q: queue.Queue[Any] = queue.Queue(maxsize=queue_max)
        self._seq: int = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started: bool = False
        self._closing: bool = False
        self._warm = threading.Event()

        self._stream: Any = None
        self._target_sr: int = 0
        self._device_cur: Any = None
        self._resampler: Any = None
        self._carry: bytes = b""
        self._cur_fmt: Any = None
        self._play_seq: int = 0

    def _log(self, msg: str) -> None:
        if self._on_log is None:
            return
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_log, msg)
        else:
            self._on_log(msg)

    def prewarm(self, device: Any) -> None:
        with self._lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                return
            self._closing = False
            self._q = queue.Queue(maxsize=self._queue_max)
            stream = self._open_stream(device)
            stream.start()
            self._stream = stream
            self._device_cur = device
            self._warm.clear()
            t = threading.Thread(target=self._run, daemon=True)
            self._thread = t
            self._started = True
            t.start()
        self._warm.wait(timeout=5.0)

    def _open_stream(self, device: Any) -> Any:
        resolved = self._resolve_device(device)
        self._target_sr = self._get_target_sr(resolved)
        if self._stream_factory is not None:
            return self._stream_factory(
                samplerate=self._target_sr,
                channels=1,
                dtype="float32",
                device=resolved,
                latency="low",
            )
        import sounddevice as sd

        return sd.OutputStream(
            samplerate=self._target_sr,
            channels=1,
            dtype="float32",
            device=resolved,
            latency="low",
        )

    def _resolve_device(self, device: Any) -> Any:
        if device is None:
            from .devices import preferred_default_output

            return preferred_default_output()
        return device

    def _get_target_sr(self, device: Any) -> int:
        if self._stream_factory is not None:
            return 48000
        import sounddevice as sd

        return _device_samplerate(sd, device)

    def _run(self) -> None:
        keepalive_frames = max(1, int(self._target_sr * self._keepalive_ms / 1000))
        silence = np.zeros(keepalive_frames, dtype=np.float32)
        warm_min_frames = int(self._target_sr * self._warm_min_ms / 1000)
        warmed_frames = 0
        max_write_frames = max(1, int(self._target_sr * self._max_write_ms / 1000))
        try:
            while True:
                try:
                    item = self._q.get(timeout=self._keepalive_ms / 1000.0)
                except queue.Empty:
                    if self._stream is not None:
                        t0 = time.monotonic()
                        self._stream.write(silence)
                        blk = (time.monotonic() - t0) * 1000
                        if blk > self._keepalive_ms * 3:
                            self._log(f"keepalive write blocked {blk:.0f}ms")
                        warmed_frames += keepalive_frames
                        if warmed_frames >= warm_min_frames and not self._warm.is_set():
                            self._warm.set()
                    continue
                if item is _CLOSE:
                    break
                if item is _ABORT:
                    self._carry = b""
                    self._reset_resampler()
                    continue
                if isinstance(item, _Begin):
                    device_changed = item.device != self._device_cur
                    need_reopen = device_changed
                    if need_reopen:
                        self._reopen(item.device)
                    self._cur_fmt = item.fmt
                    self._make_resampler(item.fmt)
                    self._carry = b""
                    self._play_seq = item.seq
                    continue
                if isinstance(item, _End):
                    if item.seq == self._play_seq:
                        tail = self._flush_resampler()
                        if tail is not None and len(tail) > 0:
                            self._stream.write(tail)
                    continue
                if isinstance(item, _Audio):
                    if item.seq != self._play_seq:
                        continue
                    frames = self._decode_resample(item.data)
                    if frames is not None and len(frames) > 0:
                        self._write_chunked(frames, max_write_frames)
        except Exception as exc:
            self._log(f"playback worker error: {exc}")
        finally:
            with self._lock:
                if self._stream is not None:
                    try:
                        self._stream.stop()
                    except Exception:
                        pass
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                self._stream = None
                self._started = False
                self._closing = False
                self._thread = None

    def _write_chunked(self, frames: np.ndarray, max_write_frames: int) -> None:
        total = len(frames)
        offset = 0
        # 協調 abort: 現発話 seq(_play_seq) より新しい begin/abort が来た(_seq が進んだ)ら残りを捨てる。
        # abort() は _seq を ++ するので、再生中チャンクの途中でも次サブwrite前に中断できる。
        play_seq = self._play_seq
        while offset < total:
            if self._seq != play_seq:
                break
            end = min(offset + max_write_frames, total)
            chunk = frames[offset:end]
            self._stream.write(chunk)
            offset = end

    def _reopen(self, device: Any) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
        stream = self._open_stream(device)
        stream.start()
        self._stream = stream
        self._device_cur = device
        self._warm.clear()

    def _make_resampler(self, fmt: Any) -> None:
        if fmt is not None and fmt.channels > 1:
            self._log(f"rejecting multi-channel audio: channels={fmt.channels}")
        self._resampler = None
        if fmt is not None and fmt.sample_rate != self._target_sr:
            if self._resampler_factory is not None:
                self._resampler = self._resampler_factory(
                    fmt.sample_rate, self._target_sr, 1
                )
            else:
                import soxr

                self._resampler = soxr.ResampleStream(
                    fmt.sample_rate, self._target_sr, 1, dtype="float32", quality="HQ"
                )

    def _reset_resampler(self) -> None:
        self._resampler = None
        if self._cur_fmt is not None and self._cur_fmt.sample_rate != self._target_sr:
            if self._resampler_factory is not None:
                self._resampler = self._resampler_factory(
                    self._cur_fmt.sample_rate, self._target_sr, 1
                )
            else:
                import soxr

                self._resampler = soxr.ResampleStream(
                    self._cur_fmt.sample_rate, self._target_sr, 1, dtype="float32", quality="HQ"
                )

    def _flush_resampler(self) -> np.ndarray | None:
        if self._resampler is None:
            return None
        empty = np.empty((0,), dtype=np.float32)
        return self._resampler.resample_chunk(empty, last=True)

    def _decode_resample(self, data: bytes) -> np.ndarray | None:
        if self._cur_fmt is None:
            return None
        encoding = self._cur_fmt.codec if self._cur_fmt.codec else "pcm_s16le"
        frame_bytes = bytes_per_sample(encoding) * 1
        full, self._carry = split_full_samples(self._carry + data, frame_bytes)
        if not full:
            return None
        frames = _pcm_to_float32(full, encoding, 1)
        if self._resampler is not None:
            frames = self._resampler.resample_chunk(frames)
        return frames

    def begin(self, fmt: Any, device: Any) -> None:
        with self._lock:
            if not self._started or self._thread is None or not self._thread.is_alive():
                self._closing = False
                self._q = queue.Queue(maxsize=self._queue_max)
                stream = self._open_stream(device)
                stream.start()
                self._stream = stream
                self._device_cur = device
                self._warm.clear()
                t = threading.Thread(target=self._run, daemon=True)
                self._thread = t
                self._started = True
                self._seq += 1
                t.start()
            else:
                self._seq += 1
        self._q.put(_Begin(self._seq, fmt, device))

    def feed(self, chunk: bytes) -> bool:
        try:
            self._q.put_nowait(_Audio(self._seq, chunk))
            return True
        except Full:
            return False

    def end(self) -> None:
        try:
            self._q.put_nowait(_End(self._seq))
        except Full:
            pass

    def abort(self) -> None:
        with self._lock:
            self._seq += 1
        try:
            self._q.put_nowait(_ABORT)
        except Full:
            pass

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
        try:
            self._q.put_nowait(_CLOSE)
        except Full:
            pass

    def join(self, timeout: float = 2.0) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def set_device(self, device: Any) -> None:
        with self._lock:
            if not self._started or self._thread is None or not self._thread.is_alive():
                return
        self._q.put(_Begin(self._seq + 1, self._cur_fmt, device))
        with self._lock:
            self._seq += 1
