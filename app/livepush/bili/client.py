"""B 站直播状态采集客户端。

只做三件事：
1. 批量查直播状态（`get_status_info_by_uids`，免登录，按 100 个 uid 分块）；
2. 把用户输入的 UID / 房间号 / 短号 / 链接解析成直播间；
3. 封面下载缓存。

风控策略：-352 降频、-412 熔断（IP 级封锁），绝不重试风暴。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .covers import DEFAULT_UA, CoverCache
from .parse import RoomSnapshot, parse_room_info_payload, parse_room_init, parse_status_item

API_BASE = "https://api.live.bilibili.com"
RISK_CODES = {-352, -412, -509, -799, 412}
BATCH_LIMIT = 100  # 实测超过 100 个 uid 会 HTTP 400


class BiliError(Exception):
    """接口返回业务错误或网络错误。"""


class BiliRiskControl(BiliError):
    """触发了 B 站风控，需要冷却。"""


class BiliNotFound(BiliError):
    """房间/用户不存在或无法解析。"""


@dataclass(slots=True)
class ResolvedRoom:
    uid: int
    room_id: int = 0
    short_id: int = 0
    uname: str = ""
    face: str = ""
    title: str = ""
    live_status: int = 0
    cover_url: str = ""
    area_name: str = ""
    area_parent: str = ""

    @classmethod
    def from_snapshot(cls, snap: RoomSnapshot) -> "ResolvedRoom":
        return cls(
            uid=snap.uid,
            room_id=snap.room_id,
            short_id=snap.short_id,
            uname=snap.uname,
            face=snap.face,
            title=snap.title,
            live_status=snap.live_status,
            cover_url=snap.cover_url,
            area_name=snap.area_name,
            area_parent=snap.area_parent,
        )

    @property
    def display_room_id(self) -> int:
        return self.room_id or self.short_id or self.uid


def extract_id(text: str) -> int | None:
    """从 UID / 房间号 / 链接里抠出数字。

    注意不能要求「至少 3 位」：B 站存在很小的 uid（例如 uid=2）和很小的房间号。
    解析结果会在 UI 里先预览，用户确认后才落库，所以这里可以宽松一点。
    """
    if not text:
        return None
    raw = text.strip()
    match = re.search(r"(?:space\.bilibili\.com/|live\.bilibili\.com/|/)(\d+)", raw)
    if match:
        return int(match.group(1))
    match = re.search(r"\d+", raw)
    return int(match.group(0)) if match else None


class BiliClient:
    def __init__(
        self,
        covers_dir: Path,
        cookie: str = "",
        proxy: str = "",
        timeout: float = 10.0,
        chunk_delay: float = 0.4,
        user_agent: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {
            "User-Agent": user_agent or DEFAULT_UA,
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        if cookie:
            headers["Cookie"] = cookie
        kwargs: dict = {"headers": headers, "timeout": timeout, "follow_redirects": True}
        if proxy:
            kwargs["proxy"] = proxy
        if transport is not None:  # 测试注入
            kwargs["transport"] = transport
        self._http = httpx.AsyncClient(base_url=API_BASE, **kwargs)
        self._chunk_delay = max(0.0, chunk_delay)
        self._blocked_until = 0.0
        self.risk_reason = ""
        self.covers = CoverCache(covers_dir)
        self._fetch_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 基础
    @property
    def blocked(self) -> bool:
        return time.time() < self._blocked_until

    @property
    def blocked_remaining(self) -> int:
        return max(0, int(self._blocked_until - time.time()))

    def _trip(self, seconds: int, reason: str) -> None:
        self._blocked_until = time.time() + seconds
        self.risk_reason = reason

    def clear_risk(self) -> None:
        self._blocked_until = 0.0
        self.risk_reason = ""

    async def _get(self, path: str, params=None) -> dict:
        if self.blocked:
            raise BiliRiskControl(f"风控冷却中，剩余 {self.blocked_remaining} 秒：{self.risk_reason}")
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise BiliError(f"网络错误：{exc}") from exc

        if resp.status_code == 412:
            self._trip(1800, "HTTP 412 请求被拦截（IP 级风控）")
            raise BiliRiskControl("HTTP 412，已熔断 30 分钟")
        if resp.status_code == 429:
            self._trip(600, "HTTP 429 请求过多")
            raise BiliRiskControl("HTTP 429，已熔断 10 分钟")
        if resp.status_code >= 400:
            raise BiliError(f"HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise BiliError("返回内容不是 JSON（可能被风控页面替换）") from exc
        if not isinstance(payload, dict):
            raise BiliError("返回结构异常")

        code = payload.get("code", 0)
        if code != 0:
            message = payload.get("message") or payload.get("msg") or ""
            if code in RISK_CODES:
                cooldown = 1800 if code in (-412, 412) else 300
                self._trip(cooldown, f"code={code} {message}")
                raise BiliRiskControl(f"风控 code={code}，已熔断 {cooldown // 60} 分钟")
            raise BiliError(f"code={code} {message}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 批量状态
    async def get_status_by_uids(
        self,
        uids: list[int],
        chunk_size: int = 50,
        chunk_delay: float | None = None,
    ) -> dict[int, RoomSnapshot]:
        """返回「能查到的」快照；查询成功但缺失的 uid 代表该用户没有直播间。"""
        result: dict[int, RoomSnapshot] = {}
        if not uids:
            return result
        size = max(1, min(chunk_size, BATCH_LIMIT))
        delay = self._chunk_delay if chunk_delay is None else max(0.0, chunk_delay)
        async with self._fetch_lock:
            for index in range(0, len(uids), size):
                chunk = uids[index : index + size]
                params = [("uids[]", str(uid)) for uid in chunk]
                data = await self._get("/room/v1/Room/get_status_info_by_uids", params=params)
                for key, raw in data.items():
                    try:
                        uid = int(key)
                    except (TypeError, ValueError):
                        continue
                    snap = parse_status_item(uid, raw)
                    if snap is not None:
                        result[snap.uid] = snap
                if index + size < len(uids) and delay:
                    await asyncio.sleep(delay)
        self.clear_risk()
        return result

    # ------------------------------------------------------------------ 解析输入
    async def get_room_info(self, room_or_short_id: int) -> RoomSnapshot:
        """按房间号/短号解析出完整快照。"""
        data = await self._get("/room/v1/Room/room_init", params={"room_id": room_or_short_id})
        room_id, short_id, uid = parse_room_init(data)
        if not uid and not room_id:
            raise BiliNotFound(f"找不到房间 {room_or_short_id}")
        target_room = room_id or room_or_short_id

        try:
            payload = await self._get("/xlive/web-room/v1/index/getInfoByRoom", params={"room_id": target_room})
            snap = parse_room_info_payload(payload)
        except BiliError:
            snap = None
        if snap is not None and snap.uid:
            if not snap.short_id:
                snap.short_id = short_id
            return snap

        # 退回批量接口按 uid 取
        try:
            snaps = await self.get_status_by_uids([uid], chunk_size=1, chunk_delay=0)
        except BiliError:
            snaps = {}
        if uid in snaps:
            snap = snaps[uid]
            snap.short_id = snap.short_id or short_id
            return snap

        # 最差情况：只有昵称与头像
        info = await self.get_master_info(uid)
        return RoomSnapshot(
            uid=uid,
            room_id=target_room,
            short_id=short_id,
            uname=info.get("uname", ""),
            face=info.get("face", ""),
        )

    async def get_master_info(self, uid: int) -> dict:
        try:
            data = await self._get("/live_user/v1/Master/info", params={"uid": uid})
        except BiliError:
            return {}
        info = data.get("info") or {}
        return {"uname": str(info.get("uname") or ""), "face": str(info.get("face") or "")}

    async def resolve(self, text: str) -> ResolvedRoom:
        """把用户输入解析成直播间：先当 UID，再当房间号。"""
        number = extract_id(text)
        if number is None:
            raise BiliNotFound("无法从输入解析出 UID 或房间号")
        try:
            snaps = await self.get_status_by_uids([number], chunk_size=1, chunk_delay=0)
        except BiliRiskControl:
            raise
        except BiliError:
            snaps = {}
        if number in snaps:
            return ResolvedRoom.from_snapshot(snaps[number])
        snap = await self.get_room_info(number)
        return ResolvedRoom.from_snapshot(snap)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self.covers.aclose()
