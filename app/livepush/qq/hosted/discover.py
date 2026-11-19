"""在候选端口上探测 OneBot11：发 `get_login_info` 拿回 self_id 就算找到了。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

log = logging.getLogger("livepush.hosted.discover")

PROBE_ECHO = "livepush-probe"


async def probe_onebot11(ws_url: str, token: str = "", timeout: float = 6.0) -> dict | None:
    """返回 {"self_id": int, "nickname": str}；连不上/不是 OneBot11 返回 None。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        async with connect(
            ws_url,
            additional_headers=headers,
            open_timeout=timeout,
            close_timeout=1,
            max_size=1 << 20,
        ) as ws:
            await ws.send(
                json.dumps({"action": "get_login_info", "params": {}, "echo": PROBE_ECHO}, ensure_ascii=False)
            )
            while True:
                remain = deadline - loop.time()
                if remain <= 0:
                    return None
                raw = await asyncio.wait_for(ws.recv(), timeout=remain)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if payload.get("echo") != PROBE_ECHO:
                    continue
                if payload.get("status") != "ok" and payload.get("retcode") not in (0, None):
                    return None
                data = payload.get("data") or {}
                self_id = int(data.get("user_id") or 0)
                nickname = str(data.get("nickname") or "")
                return {"self_id": self_id, "nickname": nickname}
    except (OSError, asyncio.TimeoutError, WebSocketException, ValueError) as exc:
        log.debug("探测 %s 失败：%s", ws_url, exc)
        return None


def candidate_urls(ports: Iterable[int]) -> list[str]:
    return [f"ws://127.0.0.1:{port}" for port in ports if port]


async def wait_for_onebot11(
    urls: list[str],
    token: str = "",
    *,
    timeout: float = 120.0,
    interval: float = 2.0,
) -> tuple[str, dict] | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for url in urls:
            remain = deadline - loop.time()
            if remain <= 0:
                return None
            info = await probe_onebot11(url, token, timeout=min(6.0, remain))
            if info:
                return url, info
        await asyncio.sleep(interval)
    return None
