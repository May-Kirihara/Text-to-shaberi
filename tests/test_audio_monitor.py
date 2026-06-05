"""AudioMonitorServer（全合成音声 broadcast）のテスト。

FakeConn を _handle に流して購読者ライフサイクル・backpressure・stall切断・
mid-stream join・close を検証する。
"""
import asyncio
import json
from types import SimpleNamespace

from text_to_shaberi.client.audio_monitor import AudioMonitorServer

_STOP = object()
_FMT = {"codec": "pcm_s16le", "sample_rate": 48000, "channels": 1}


class FakeConn:
    def __init__(self, *, block_send: bool = False) -> None:
        self.sent: list = []
        self.closed = False
        self.request = SimpleNamespace(path="/", headers={})
        self._q: asyncio.Queue = asyncio.Queue()
        self._gate = asyncio.Event()
        if not block_send:
            self._gate.set()

    async def send(self, data) -> None:
        await self._gate.wait()
        self.sent.append(data)

    async def close(self, code=None, reason=None) -> None:
        self.closed = True
        self._q.put_nowait(_STOP)

    def unblock(self) -> None:
        self._gate.set()

    def disconnect(self) -> None:
        self._q.put_nowait(_STOP)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is _STOP:
            raise StopAsyncIteration
        return item


async def _settle() -> None:
    for _ in range(6):
        await asyncio.sleep(0.005)


def _events(conn: FakeConn) -> list:
    out: list = []
    for m in conn.sent:
        if isinstance(m, (bytes, bytearray)):
            out.append(("bin", bytes(m)))
        else:
            out.append(json.loads(m))
    return out


def test_broadcast_to_multiple_subscribers_in_order():
    async def go():
        srv = AudioMonitorServer(queue_max=64)
        c1, c2 = FakeConn(), FakeConn()
        t1 = asyncio.ensure_future(srv._handle(c1))
        t2 = asyncio.ensure_future(srv._handle(c2))
        await _settle()
        srv.broadcast("begin", _FMT)
        srv.broadcast("chunk", b"\x01\x02")
        srv.broadcast("chunk", b"\x03\x04")
        srv.broadcast("end", None)
        await _settle()
        for c in (c1, c2):
            ev = _events(c)
            assert ev[0] == {"event": "audio_begin", "format": _FMT}
            assert ev[1] == ("bin", b"\x01\x02")
            assert ev[2] == ("bin", b"\x03\x04")
            assert ev[3] == {"event": "audio_end"}
        c1.disconnect()
        c2.disconnect()
        await asyncio.gather(t1, t2)
        await srv.close()

    asyncio.run(go())


def test_broadcast_zero_subscribers_noop():
    async def go():
        srv = AudioMonitorServer()
        srv.broadcast("begin", _FMT)
        srv.broadcast("chunk", b"\x00")
        srv.broadcast("end", None)
        assert srv._cur_format is None  # end が format をクリア
        await srv.close()

    asyncio.run(go())


def test_chunk_drop_keeps_begin_end():
    async def go():
        srv = AudioMonitorServer(queue_max=3)
        c = FakeConn(block_send=True)  # sender は最初の send で停止
        t = asyncio.ensure_future(srv._handle(c))
        await _settle()
        srv.broadcast("begin", _FMT)
        for i in range(10):
            srv.broadcast("chunk", bytes([i]))
        srv.broadcast("end", None)
        await _settle()
        c.unblock()
        await _settle()
        ev = _events(c)
        assert ev[0] == {"event": "audio_begin", "format": _FMT}
        assert ev[-1] == {"event": "audio_end"}
        n_chunks = sum(1 for e in ev if isinstance(e, tuple))
        assert n_chunks < 10  # chunk が間引かれた
        assert c in srv._subs  # chunk を退避できる間は drop されない
        c.disconnect()
        await t
        await srv.close()

    asyncio.run(go())


def test_stall_subscriber_dropped_others_unaffected():
    async def go():
        srv = AudioMonitorServer(queue_max=2)
        slow = FakeConn(block_send=True)
        fast = FakeConn()
        ts = asyncio.ensure_future(srv._handle(slow))
        tf = asyncio.ensure_future(srv._handle(fast))
        await _settle()
        # fast は毎 settle で drain、slow は send 停止で制御イベントが溜まり続ける。
        srv.broadcast("begin", _FMT)
        srv.broadcast("end", None)
        await _settle()  # slow: sender が begin を pop して送信ブロック → slow.q=[end]
        srv.broadcast("begin", _FMT)  # slow.q=[end,begin] 満杯
        srv.broadcast("end", None)  # slow: 満杯+退避chunk無し → drop。fast: drain。
        await _settle()
        assert slow not in srv._subs  # 停滞購読者は切断
        assert fast in srv._subs  # 健全な購読者は無傷
        # fast はその後も受信を継続できる
        srv.broadcast("chunk", b"\x09")
        srv.broadcast("end", None)
        await _settle()
        ev = _events(fast)
        assert ("bin", b"\x09") in ev
        assert ev[-1] == {"event": "audio_end"}
        fast.disconnect()
        slow.unblock()
        await asyncio.gather(tf, ts, return_exceptions=True)
        await srv.close()

    asyncio.run(go())


def test_midstream_join_gets_begin():
    async def go():
        srv = AudioMonitorServer(queue_max=64)
        c1 = FakeConn()
        t1 = asyncio.ensure_future(srv._handle(c1))
        await _settle()
        srv.broadcast("begin", _FMT)
        srv.broadcast("chunk", b"\xaa")
        await _settle()
        c2 = FakeConn()  # 発話の途中で参加
        t2 = asyncio.ensure_future(srv._handle(c2))
        await _settle()
        srv.broadcast("chunk", b"\xbb")
        srv.broadcast("end", None)
        await _settle()
        ev2 = _events(c2)
        assert ev2[0] == {"event": "audio_begin", "format": _FMT}  # begin 補完
        assert ("bin", b"\xbb") in ev2
        assert ev2[-1] == {"event": "audio_end"}
        assert ("bin", b"\xaa") not in ev2  # 参加前の chunk は受け取らない
        c1.disconnect()
        c2.disconnect()
        await asyncio.gather(t1, t2)
        await srv.close()

    asyncio.run(go())


def test_close_cancels_senders():
    async def go():
        srv = AudioMonitorServer()
        c1, c2 = FakeConn(), FakeConn()
        t1 = asyncio.ensure_future(srv._handle(c1))
        t2 = asyncio.ensure_future(srv._handle(c2))
        await _settle()
        assert len(srv._subs) == 2
        await srv.close()
        assert len(srv._subs) == 0
        assert srv._server is None
        # broadcast 後は no-op（_closing）
        srv.broadcast("begin", _FMT)
        c1.disconnect()
        c2.disconnect()
        await asyncio.gather(t1, t2, return_exceptions=True)
        await _settle()

    asyncio.run(go())
