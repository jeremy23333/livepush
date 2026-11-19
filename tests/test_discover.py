"""OneBot11 端口探测：认识「哪些端口是真的协议端」。"""

from __future__ import annotations

import json

import pytest
from websockets.asyncio.server import serve

from livepush.qq.hosted.discover import PROBE_ECHO, candidate_urls, probe_onebot11, wait_for_onebot11


async def _onebot_handler(ws):
    async for raw in ws:
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if req.get("action") != "get_login_info":
            continue
        await ws.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 10001, "nickname": "小号"},
                    "echo": req.get("echo"),
                },
                ensure_ascii=False,
            )
        )


@pytest.mark.asyncio
async def test_probe_recognizes_onebot11():
    async with serve(_onebot_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        info = await probe_onebot11(f"ws://127.0.0.1:{port}", timeout=3)
        assert info == {"self_id": 10001, "nickname": "小号"}


@pytest.mark.asyncio
async def test_probe_returns_none_when_nothing_listens():
    assert await probe_onebot11("ws://127.0.0.1:9", timeout=1) is None


@pytest.mark.asyncio
async def test_probe_ignores_unrelated_websocket():
    async def handler(ws):
        await ws.send(json.dumps({"hello": "world"}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert await probe_onebot11(f"ws://127.0.0.1:{port}", timeout=1.5) is None


@pytest.mark.asyncio
async def test_wait_for_onebot11_skips_dead_port():
    async with serve(_onebot_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        found = await wait_for_onebot11(
            candidate_urls([9, port]), "", timeout=6, interval=0.2
        )
        assert found is not None
        url, info = found
        assert url == f"ws://127.0.0.1:{port}"
        assert info["self_id"] == 10001


def test_probe_echo_is_stable():
    # 探测帧的 echo 值若被改动，老测试和日志定位都会失效
    assert PROBE_ECHO == "livepush-probe"
