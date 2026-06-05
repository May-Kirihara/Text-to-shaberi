"""test_local_input_server: LocalInputServer の WS 挙動テスト（実 websockets）。"""
from __future__ import annotations

import asyncio
import json

import websockets

from text_to_shaberi.client.text_input import LocalInputServer


def _port(srv):
    return srv._server.sockets[0].getsockname()[1]


def test_text_and_final_callbacks():
    async def go():
        rec_text = []
        rec_final = []
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: rec_text.append((t, p)),
            on_final=lambda: rec_final.append(1),
            on_cancel=lambda: None,
        )
        await srv.start()
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            await c.send(json.dumps({"text": "あ"}))
            r1 = json.loads(await c.recv())
            await c.send(json.dumps({"final": True}))
            r2 = json.loads(await c.recv())
            assert r1 == {"event": "accepted"}
            assert r2 == {"event": "accepted"}
        await asyncio.sleep(0.05)
        assert rec_text == [("あ", None)]
        assert len(rec_final) >= 1
        await srv.close()

    asyncio.run(go())


def test_unauthorized_closes():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            token="secret",
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        raised = False
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            try:
                await asyncio.wait_for(c.recv(), timeout=2.0)
            except websockets.ConnectionClosed:
                raised = True
        assert raised
        await srv.close()

    asyncio.run(go())


def test_busy_second_connection():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        url = f"ws://127.0.0.1:{_port(srv)}"
        async with websockets.connect(url) as c1:
            await c1.send(json.dumps({"text": "x"}))
            await c1.recv()
            async with websockets.connect(url) as c2:
                msg = json.loads(await c2.recv())
                assert msg == {"event": "error", "message": "busy"}
        await srv.close()

    asyncio.run(go())


def test_invalid_json():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            await c.send("{not json")
            msg = json.loads(await c.recv())
            assert msg["event"] == "error"
        await srv.close()

    asyncio.run(go())


def test_notify_done():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            await c.send(json.dumps({"text": "x"}))
            await c.recv()
            srv.mark_active()  # request 開始 → done の宛先(_audio_conn)を固定
            await srv.notify({"event": "done"})
            msg = json.loads(await c.recv())
            assert msg == {"event": "done"}
        await srv.close()

    asyncio.run(go())


def test_nonloopback_requires_token():
    async def go():
        srv = LocalInputServer(
            host="0.0.0.0",
            port=0,
            token="",
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        raised = False
        try:
            await srv.start()
        except RuntimeError:
            raised = True
        assert raised

    asyncio.run(go())


def test_audio_out_order_begin_chunk_end_then_done():
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        async with websockets.connect(f"ws://127.0.0.1:{_port(srv)}") as c:
            await c.send(json.dumps({"text": "x"}))
            assert json.loads(await c.recv()) == {"event": "accepted"}
            srv.mark_active()  # app が StreamStart 時に呼ぶ＝request 開始で宛先固定
            # tap からの音声 → 同一 queue で順序化、done は audio_end の後
            srv.send_audio("begin", {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1})
            srv.send_audio("chunk", b"\x01\x02\x03\x04")
            srv.send_audio("end", None)
            await srv.notify({"event": "done"})
            m1 = json.loads(await c.recv())
            assert m1 == {"event": "audio_begin", "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}}
            assert (await c.recv()) == b"\x01\x02\x03\x04"
            m3 = json.loads(await c.recv())
            assert m3 == {"event": "audio_end"}
            m4 = json.loads(await c.recv())
            assert m4 == {"event": "done"}
        await srv.close()

    asyncio.run(go())


def test_audio_pinned_to_request_conn_no_leak():
    # A が text 投入後に切断 → reservation 継続で B は busy。A 由来音声は B へ漏れない。
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        port = _port(srv)
        a = await websockets.connect(f"ws://127.0.0.1:{port}")
        await a.send(json.dumps({"text": "x"}))
        assert json.loads(await a.recv()) == {"event": "accepted"}
        srv.mark_active()  # request 開始 → reserved=A
        await a.close()
        await asyncio.sleep(0.05)
        b = await websockets.connect(f"ws://127.0.0.1:{port}")
        assert json.loads(await b.recv()) == {"event": "error", "message": "busy"}
        # A 由来の音声を流しても、宛先は閉じた A → drop。B には来ない。
        srv.send_audio("begin", {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1})
        srv.send_audio("chunk", b"\x01\x02")
        srv.send_audio("end", None)
        leaked = False
        try:
            await asyncio.wait_for(b.recv(), timeout=0.3)
            leaked = True
        except Exception:
            pass
        assert not leaked
        await srv.close()

    asyncio.run(go())


def test_reconnect_allowed_after_done():
    # done で reservation が解除され、owner 切断後でも新接続が受理される（永久 busy にならない）。
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        port = _port(srv)
        a = await websockets.connect(f"ws://127.0.0.1:{port}")
        await a.send(json.dumps({"text": "x"}))
        assert json.loads(await a.recv()) == {"event": "accepted"}
        srv.mark_active()
        await a.close()
        await asyncio.sleep(0.05)
        await srv.notify({"event": "done"})  # reservation 解除
        await asyncio.sleep(0.02)
        b = await websockets.connect(f"ws://127.0.0.1:{port}")
        await b.send(json.dumps({"text": "y"}))
        assert json.loads(await b.recv()) == {"event": "accepted"}  # busy ではない
        await b.close()
        await srv.close()

    asyncio.run(go())


def test_reserve_only_via_mark_active_not_on_plain_text():
    # mark_active を呼ばなければ（空 text 相当）予約は立たず、別接続が busy にならない。
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        await srv.start()
        port = _port(srv)
        a = await websockets.connect(f"ws://127.0.0.1:{port}")
        await a.send(json.dumps({"text": ""}))  # 空 text → app は mark_active を呼ばない
        assert json.loads(await a.recv()) == {"event": "accepted"}
        assert srv._reserved is False
        await a.close()
        await asyncio.sleep(0.05)
        b = await websockets.connect(f"ws://127.0.0.1:{port}")
        await b.send(json.dumps({"text": "y"}))
        assert json.loads(await b.recv()) == {"event": "accepted"}  # 永久 busy にならない
        await b.close()
        await srv.close()

    asyncio.run(go())


def test_cancel_acks_and_reservation_released_via_done():
    # cancel は on_cancel(→app では bridge.abort) を呼び ack を返す。reservation の解除は
    # bridge が音声終了後に出す done(notify) が担う（codex#4: 早期解除で leak しないため）。
    async def go():
        cancels = []
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: cancels.append(1),
        )
        await srv.start()
        port = _port(srv)
        a = await websockets.connect(f"ws://127.0.0.1:{port}")
        await a.send(json.dumps({"text": "x"}))
        assert json.loads(await a.recv()) == {"event": "accepted"}
        srv.mark_active()  # reserved=A
        await a.send(json.dumps({"type": "cancel"}))
        assert json.loads(await a.recv()) == {"event": "accepted"}  # cancel ack
        assert cancels == [1]
        assert srv._reserved is True  # cancel 単体では解除しない
        await srv.notify({"event": "done"})  # bridge 相当の done
        await asyncio.sleep(0.02)
        assert srv._reserved is False
        await a.close()
        await srv.close()

    asyncio.run(go())


def test_control_events_not_dropped_on_overflow():
    # out_max を小さくして溢れさせても begin/end/done(制御) は捨てられず chunk のみ drop。
    async def go():
        srv = LocalInputServer(
            host="127.0.0.1",
            port=0,
            out_max=4,
            on_text=lambda t, p: None,
            on_final=lambda: None,
            on_cancel=lambda: None,
        )
        srv._audio_conn = object()  # 宛先ダミー（None ガード回避）。sender は起動しない。
        srv.send_audio("begin", {"x": 1})
        for _ in range(30):
            srv.send_audio("chunk", b"zz")
        srv.send_audio("end", None)
        srv._enqueue(srv._audio_conn, "json", {"event": "done"})
        kinds = [k for (_c, k, _p) in srv._out_q]
        assert "begin" in kinds
        assert "end" in kinds
        assert "json" in kinds
        assert kinds.count("chunk") <= 4

    asyncio.run(go())
