"""OneBot v11 正向 WebSocket 客户端。

一条连接同时收发：API 请求靠 `echo` 关联，事件直接推送过来。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Any

import websockets
from websockets.asyncio.client import connect

from .base import BotClient, SendResult
from .message import Message, to_onebot11

log = logging.getLogger("livepush.onebot11")

# 这些 retcode / 文案代表目标本身不可用
_FATAL_HINTS = ("GROUP_NOT_FOUND", "USER_NOT_FOUND", "群不存在", "not found")
_RISK_HINTS = ("SEND_MSG_API_ERROR", "风控", "risk")


class OneBotError(Exception):
    def __init__(self, message: str, retcode: int = 0) -> None:
        super().__init__(message)
        self.retcode = retcode


class OneBot11Client(BotClient):
    transport = "onebot11"

    def __init__(self, conn_id: int, name: str, ws_url: str, token: str = "", timeout: float = 30.0) -> None:
        super().__init__(conn_id, name)
        self.ws_url = ws_url
        self.token = token
        self.timeout = timeout
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._echo = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._backoff = 1.0
        self.last_event_at = 0
        self.last_heartbeat_at = 0
        self.heartbeat_status: dict = {}
        self.last_notice: dict = {}
        self._ready = asyncio.Event()

    # ------------------------------------------------------------------ 连接
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._ready.clear()
        self.state = "connecting"
        self._task = asyncio.create_task(self._run(), name=f"onebot-{self.conn_id}")
        # 等连接真正建立，避免调用方紧接着 call() 撞上「连接不可用」
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("OneBot 15 秒内未连上：%s", self.ws_url)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._fail_pending("客户端已停止")
        self.state = "stopped"

    async def _run(self) -> None:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        while not self._stop.is_set():
            try:
                async with connect(self.ws_url, additional_headers=headers, open_timeout=15, ping_interval=30) as ws:
                    self._ws = ws
                    self._backoff = 1.0
                    await self._on_connected()
                    async for raw in ws:
                        await self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state = "disconnected"
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("OneBot 连接中断：%s", self.last_error)
            finally:
                self._ws = None
                self._ready.clear()
                self._fail_pending("连接已断开")
            if self._stop.is_set():
                break
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 60.0)

    async def _on_connected(self) -> None:
        self.state = "connected"
        self.last_error = ""
        self._ready.set()
        log.info("OneBot 已连接：%s", self.ws_url)
        try:
            info = await self.call("get_login_info", {})
            self.self_id = int(info.get("user_id") or 0)
            self.nickname = str(info.get("nickname") or "")
        except OneBotError as exc:
            log.warning("获取登录信息失败：%s", exc)

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        echo = payload.get("echo")
        if echo is not None and echo in self._pending:
            future = self._pending.pop(echo)
            if not future.done():
                future.set_result(payload)
            return
        # 事件：心跳与上下线通知是掉线探测的主要依据
        post_type = payload.get("post_type")
        if post_type == "meta_event":
            now = int(time.time())
            self.last_event_at = now
            if payload.get("meta_event_type") == "heartbeat":
                self.last_heartbeat_at = now
                status = payload.get("status")
                self.heartbeat_status = status if isinstance(status, dict) else {}
        elif post_type == "notice":
            self.last_notice = {
                "notice_type": payload.get("notice_type", ""),
                "sub_type": payload.get("sub_type", ""),
                "user_id": payload.get("user_id", 0),
                "time": payload.get("time", 0),
            }

    def health(self) -> dict:
        now = int(time.time())
        payload = super().health()
        status = self.heartbeat_status or {}
        payload.update(
            {
                "last_event_at": self.last_event_at,
                "last_heartbeat_at": self.last_heartbeat_at,
                "heartbeat_age": now - self.last_heartbeat_at if self.last_heartbeat_at else None,
                "heartbeat_interval": status.get("interval"),
                "heartbeat_online": status.get("online"),
                "heartbeat_good": status.get("good"),
                "last_notice": dict(self.last_notice),
            }
        )
        return payload

    def _fail_pending(self, reason: str) -> None:
        for echo, future in list(self._pending.items()):
            self._pending.pop(echo, None)
            if not future.done():
                future.set_exception(OneBotError(reason))

    # ------------------------------------------------------------------ 调用
    async def call(self, action: str, params: dict | None = None, timeout: float | None = None) -> Any:
        if self._ws is None:
            # 刚启动/正在重连：给一点时间，避免调用方拿到假失败
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=5)
            except asyncio.TimeoutError:
                raise OneBotError("连接不可用") from None
        if self._ws is None:
            raise OneBotError("连接不可用")
        echo = f"lp-{self.conn_id}-{next(self._echo)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        payload = {"action": action, "params": params or {}, "echo": echo}
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=timeout or self.timeout)
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"{action} 超时") from exc
        finally:
            self._pending.pop(echo, None)

        status = response.get("status")
        retcode = int(response.get("retcode") or 0)
        if status == "failed" or retcode not in (0, 1):
            message = str(response.get("message") or response.get("wording") or f"retcode={retcode}")
            raise OneBotError(message, retcode)
        return response.get("data")

    # ------------------------------------------------------------------ 能力
    async def send_message(self, kind: str, target_id: str, message: Message) -> SendResult:
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"
        params = {key: int(target_id), "message": to_onebot11(message)}
        try:
            await self.call(action, params)
            return SendResult.success()
        except OneBotError as exc:
            text = str(exc)
            lowered = text.lower()
            return SendResult(
                ok=False,
                error=text,
                retcode=exc.retcode,
                fatal=any(h.lower() in lowered for h in _FATAL_HINTS),
                risk=any(h.lower() in lowered for h in _RISK_HINTS),
                unknown="连接不可用" in text or "超时" in text or "已断开" in text,
            )

    async def get_group_list(self) -> list[dict]:
        try:
            data = await self.call("get_group_list", {})
        except OneBotError as exc:
            log.warning("获取群列表失败：%s", exc)
            return []
        if not isinstance(data, list):
            return []
        return [
            {
                "id": str(item.get("group_id")),
                "name": str(item.get("group_name") or ""),
                "member_count": int(item.get("member_count") or 0),
            }
            for item in data
            if item.get("group_id")
        ]

    async def get_friend_list(self) -> list[dict]:
        try:
            data = await self.call("get_friend_list", {})
        except OneBotError as exc:
            log.warning("获取好友列表失败：%s", exc)
            return []
        if not isinstance(data, list):
            return []
        return [
            {
                "id": str(item.get("user_id")),
                "name": str(item.get("nickname") or item.get("remark") or ""),
            }
            for item in data
            if item.get("user_id")
        ]

    async def get_login_info(self) -> dict | None:
        try:
            data = await self.call("get_login_info", {}, timeout=10)
        except OneBotError:
            return None
        return {"self_id": int(data.get("user_id") or 0), "nickname": str(data.get("nickname") or "")}

    async def get_at_all_remain(self, group_id: str) -> dict | None:
        try:
            data = await self.call("get_group_at_all_remain", {"group_id": int(group_id)}, timeout=10)
        except OneBotError:
            return None
        if not isinstance(data, dict):
            return None
        return {
            "can_at_all": bool(data.get("can_at_all")),
            "remain": int(data.get("remain_at_all_count_for_group") or 0),
        }
