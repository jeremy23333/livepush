"""Milky 协议客户端。

Milky 规范：API 走 HTTP `POST /api/<name>`（Bearer token），事件走 WebHook / WS / SSE。
我们用 HTTP 调 API + 由本应用提供 `/webhook/milky` 收事件（Yogurt 与 Lagrange.Milky 都支持 WebHook）。

注意：Milky 规范里**没有**「@全体剩余次数」查询，所以 `mention_all` 直接发、失败降级。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import BotClient, SendResult
from .message import Message, to_milky

log = logging.getLogger("livepush.milky")

_FATAL_RETCODES = {-404}
_FATAL_HINTS = ("group not found", "user not found", "群不存在")
_RISK_HINTS = ("risk", "风控", "freq", "频繁")


class MilkyError(Exception):
    def __init__(self, message: str, retcode: int = 0) -> None:
        super().__init__(message)
        self.retcode = retcode


class MilkyClient(BotClient):
    transport = "milky"

    def __init__(self, conn_id: int, name: str, api_url: str, token: str = "", timeout: float = 30.0) -> None:
        super().__init__(conn_id, name)
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout, headers=self._headers())
        self.last_event_at = 0

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # ------------------------------------------------------------------ 连接
    async def start(self) -> None:
        self.state = "connecting"
        info = await self.get_login_info()
        if info:
            self.self_id = int(info.get("self_id") or 0)
            self.nickname = str(info.get("nickname") or "")
            self.state = "connected"
            self.last_error = ""
        else:
            self.state = "disconnected"

    async def stop(self) -> None:
        self.state = "stopped"
        await self._http.aclose()

    async def refresh(self) -> bool:
        """重新确认登录态（协议端重启/重新扫码后调用）。"""
        info = await self.get_login_info()
        if info:
            self.self_id = int(info.get("self_id") or 0)
            self.nickname = str(info.get("nickname") or "")
            self.state = "connected"
            self.last_error = ""
            return True
        self.state = "disconnected"
        return False

    # ------------------------------------------------------------------ 调用
    async def call(self, api: str, payload: dict | None = None, timeout: float | None = None) -> Any:
        url = f"{self.api_url}/api/{api}"
        try:
            resp = await self._http.post(url, json=payload or {}, timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise MilkyError(f"连接不可用：{exc}") from exc
        if resp.status_code == 401:
            raise MilkyError("鉴权失败（检查 AccessToken）", 401)
        try:
            body = resp.json()
        except ValueError as exc:
            raise MilkyError(f"HTTP {resp.status_code} 返回非 JSON") from exc
        status = str(body.get("status") or "").lower()
        retcode = int(body.get("retcode") or 0)
        if status == "ok" or (not status and retcode == 0):
            return body.get("data")
        message = str(body.get("message") or f"retcode={retcode}")
        raise MilkyError(message, retcode)

    # ------------------------------------------------------------------ 能力
    async def send_message(self, kind: str, target_id: str, message: Message) -> SendResult:
        api = "send_group_message" if kind == "group" else "send_private_message"
        key = "group_id" if kind == "group" else "user_id"
        payload = {key: int(target_id), "message": to_milky(message)}
        try:
            await self.call(api, payload)
            return SendResult.success()
        except MilkyError as exc:
            text = str(exc)
            lowered = text.lower()
            return SendResult(
                ok=False,
                error=text,
                retcode=exc.retcode,
                fatal=exc.retcode in _FATAL_RETCODES or any(h in lowered for h in _FATAL_HINTS),
                risk=any(h in lowered for h in _RISK_HINTS),
                unknown="连接不可用" in text or "超时" in text or "已断开" in text,
            )

    async def get_group_list(self) -> list[dict]:
        try:
            data = await self.call("get_group_list", {})
        except MilkyError as exc:
            log.warning("获取群列表失败：%s", exc)
            return []
        return self._normalise_list(data, id_keys=("group_id",), name_keys=("group_name", "name"))

    async def get_friend_list(self) -> list[dict]:
        try:
            data = await self.call("get_friend_list", {})
        except MilkyError as exc:
            log.warning("获取好友列表失败：%s", exc)
            return []
        return self._normalise_list(data, id_keys=("user_id", "uin"), name_keys=("nickname", "remark", "name"))

    @staticmethod
    def _normalise_list(data: Any, id_keys: tuple[str, ...], name_keys: tuple[str, ...]) -> list[dict]:
        if isinstance(data, dict):
            data = data.get("list") or data.get("items") or []
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ident = next((item.get(k) for k in id_keys if item.get(k) not in (None, "")), None)
            if ident is None:
                continue
            name = next((str(item.get(k)) for k in name_keys if item.get(k)), "")
            out.append(
                {
                    "id": str(ident),
                    "name": name,
                    "member_count": int(item.get("member_count") or item.get("memberCount") or 0),
                }
            )
        return out

    async def get_login_info(self) -> dict | None:
        try:
            data = await self.call("get_login_info", {}, timeout=10)
        except MilkyError:
            return None
        if not isinstance(data, dict):
            return None
        ident = data.get("user_id") or data.get("uin") or data.get("self_id") or 0
        return {"self_id": int(ident), "nickname": str(data.get("nickname") or data.get("name") or "")}

    async def get_at_all_remain(self, group_id: str) -> dict | None:
        # Milky 规范没有该接口，交由派发器降级处理
        return None
