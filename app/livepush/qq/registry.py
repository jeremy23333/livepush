"""QQ 客户端注册表：按连接配置创建/复用/关闭客户端。"""

from __future__ import annotations

import logging

from .base import BotClient
from .milky import MilkyClient
from .onebot11 import OneBot11Client

log = logging.getLogger("livepush.qq")


def build_client(conn, *, ws_url: str = "", api_url: str = "") -> BotClient:
    """根据连接配置构造客户端（不启动）。"""
    transport = (conn.transport or "milky").lower()
    if transport == "onebot11":
        url = ws_url or conn.ws_url
        return OneBot11Client(conn.id, conn.name or f"bot-{conn.id}", url, conn.token or "")
    return MilkyClient(conn.id, conn.name or f"bot-{conn.id}", api_url or conn.api_url, conn.token or "")


class BotManager:
    def __init__(self) -> None:
        self._clients: dict[int, BotClient] = {}
        self._signatures: dict[int, tuple] = {}

    def _signature(self, conn) -> tuple:
        return (conn.transport, conn.mode, conn.api_url, conn.ws_url, conn.token)

    async def get(self, conn, *, autostart: bool = True) -> BotClient:
        signature = self._signature(conn)
        client = self._clients.get(conn.id)
        if client is not None and self._signatures.get(conn.id) == signature:
            return client
        if client is not None:
            await client.stop()
            self._clients.pop(conn.id, None)
        client = build_client(conn)
        self._clients[conn.id] = client
        self._signatures[conn.id] = signature
        if autostart:
            await client.start()
        return client

    def peek(self, conn_id: int) -> BotClient | None:
        return self._clients.get(conn_id)

    def connected_clients(self) -> list[BotClient]:
        return [c for c in self._clients.values() if c.connected]

    async def stop(self, conn_id: int) -> None:
        client = self._clients.pop(conn_id, None)
        self._signatures.pop(conn_id, None)
        if client is not None:
            await client.stop()

    async def stop_all(self) -> None:
        for conn_id in list(self._clients):
            await self.stop(conn_id)

    def snapshot(self) -> list[dict]:
        return [
            {
                "conn_id": c.conn_id,
                "name": c.name,
                "transport": c.transport,
                "state": c.state,
                "self_id": c.self_id,
                "nickname": c.nickname,
                "last_error": c.last_error,
            }
            for c in self._clients.values()
        ]


manager = BotManager()
