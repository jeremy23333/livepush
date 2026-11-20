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
import socket
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
    """接口返回业务错误或网络错误。

    ``stage`` 标明出错阶段（status / room_init / room_info / master_info），
    方便 Web UI 与 CLI 给出可操作的排查提示；``code`` 是 B 站业务码（可能为 None）。
    """

    def __init__(self, message: str, *, stage: str = "", code: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code


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
        kwargs: dict = {
            "headers": headers,
            "timeout": httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            "follow_redirects": True,
            # 只认设置里显式配置的代理：服务器上的 HTTP_PROXY 往往是别的服务留下的，
            # 让 httpx 自动走它会把「添加直播间」变成 502。
            "trust_env": False,
        }
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

    async def _get(self, path: str, params=None, *, stage: str = "") -> dict:
        if self.blocked:
            raise BiliRiskControl(f"风控冷却中，剩余 {self.blocked_remaining} 秒：{self.risk_reason}")
        resp: httpx.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._http.get(path, params=params)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.8)
            except httpx.HTTPError as exc:
                raise BiliError(f"网络错误：{exc}", stage=stage) from exc
        if resp is None:
            raise BiliError(f"网络错误：{last_exc}", stage=stage) from last_exc

        if resp.status_code == 412:
            self._trip(1800, "HTTP 412 请求被拦截（IP 级风控）")
            raise BiliRiskControl("HTTP 412，已熔断 30 分钟", stage=stage, code=412)
        if resp.status_code == 429:
            self._trip(600, "HTTP 429 请求过多")
            raise BiliRiskControl("HTTP 429，已熔断 10 分钟", stage=stage, code=429)
        if resp.status_code >= 400:
            raise BiliError(f"HTTP {resp.status_code}", stage=stage)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise BiliError("返回内容不是 JSON（可能被风控页面替换）", stage=stage) from exc
        if not isinstance(payload, dict):
            raise BiliError("返回结构异常", stage=stage)

        code = payload.get("code", 0)
        if code != 0:
            message = payload.get("message") or payload.get("msg") or ""
            if code in RISK_CODES:
                cooldown = 1800 if code in (-412, 412) else 300
                self._trip(cooldown, f"code={code} {message}")
                raise BiliRiskControl(
                    f"风控 code={code}，已熔断 {cooldown // 60} 分钟", stage=stage, code=code
                )
            raise BiliError(f"code={code} {message}", stage=stage, code=code)
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
                data = await self._get(
                    "/room/v1/Room/get_status_info_by_uids", params=params, stage="status"
                )
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
        try:
            data = await self._get(
                "/room/v1/Room/room_init",
                params={"room_id": room_or_short_id},
                stage="room_init",
            )
        except BiliError as exc:
            # -404 / -400 是「房间号不存在」，不该显示成网络 502
            if exc.code in (-404, -400):
                raise BiliNotFound(
                    f"B 站没有 ID 为 {room_or_short_id} 的直播间（code={exc.code}）",
                    stage="room_init",
                    code=exc.code,
                ) from exc
            raise
        room_id, short_id, uid = parse_room_init(data)
        if not uid and not room_id:
            raise BiliNotFound(f"找不到房间 {room_or_short_id}", stage="room_init")
        target_room = room_id or room_or_short_id

        try:
            payload = await self._get(
                "/xlive/web-room/v1/index/getInfoByRoom",
                params={"room_id": target_room},
                stage="room_info",
            )
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
            data = await self._get("/live_user/v1/Master/info", params={"uid": uid}, stage="master_info")
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

    async def diagnose(self, text: str) -> dict:
        """逐项体检「添加直播间」这条链路，给 Web UI / CLI 一个可读结论。"""
        steps: list[dict] = []

        def add(name: str, ok: bool, ms: float = 0.0, detail: str = "") -> None:
            steps.append({"name": name, "ok": ok, "ms": int(ms), "detail": detail})

        number = extract_id(text)
        add(
            "解析输入",
            number is not None,
            0,
            f"{text!r} → {number}" if number is not None else "没解析出 UID / 房间号 / 链接",
        )
        if number is None:
            return {
                "ok": False,
                "steps": steps,
                "summary": "输入里没有可识别的 UID、房间号或直播间链接。",
            }

        start = time.perf_counter()
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, "api.live.bilibili.com", 443, 0, socket.SOCK_STREAM
            )
            add("DNS", True, (time.perf_counter() - start) * 1000, f"{len(infos)} 条 A/AAAA 记录")
        except OSError as exc:
            add("DNS", False, (time.perf_counter() - start) * 1000, str(exc))
            return {
                "ok": False,
                "steps": steps,
                "summary": "DNS 解析失败：服务器无法解析 api.live.bilibili.com，检查 /etc/resolv.conf 或代理 DNS。",
            }

        start = time.perf_counter()
        room_id = short_id = uid = 0
        try:
            data = await self._get(
                "/room/v1/Room/room_init", params={"room_id": number}, stage="room_init"
            )
            room_id, short_id, uid = parse_room_init(data)
            add(
                "HTTP 接口",
                True,
                (time.perf_counter() - start) * 1000,
                f"room_init 成功：room_id={room_id or '?'} uid={uid or '?'}",
            )
        except BiliRiskControl as exc:
            add("HTTP 接口", False, (time.perf_counter() - start) * 1000, f"{exc}（风控，稍后再试）")
            return {"ok": False, "steps": steps, "summary": f"被 B 站风控：{exc}"}
        except BiliNotFound as exc:
            add("HTTP 接口", True, (time.perf_counter() - start) * 1000, str(exc))
            return {"ok": False, "steps": steps, "summary": f"房间号不存在：{exc}"}
        except BiliError as exc:
            add(
                "HTTP 接口",
                False,
                (time.perf_counter() - start) * 1000,
                f"{exc}（stage={exc.stage or '?'}）",
            )
            return {
                "ok": False,
                "steps": steps,
                "summary": "连不上 B 站接口，多半是服务器出网 / 代理 / 防火墙问题（不是房间号的问题）。",
            }

        if not uid:
            add("批量状态", False, 0, "room_init 没返回 uid")
            return {"ok": False, "steps": steps, "summary": "房间信息不完整，无法继续。"}

        start = time.perf_counter()
        try:
            snaps = await self.get_status_by_uids([uid], chunk_size=1, chunk_delay=0)
            hint = "（该用户当前没有直播间）" if uid not in snaps else ""
            add("批量状态", True, (time.perf_counter() - start) * 1000, f"收录 {len(snaps)} 个 uid{hint}")
        except BiliError as exc:
            add("批量状态", False, (time.perf_counter() - start) * 1000, str(exc))

        start = time.perf_counter()
        try:
            payload = await self._get(
                "/xlive/web-room/v1/index/getInfoByRoom",
                params={"room_id": room_id or number},
                stage="room_info",
            )
            snap = parse_room_info_payload(payload)
            add(
                "房间详情",
                bool(snap and snap.uid),
                (time.perf_counter() - start) * 1000,
                f"{snap.title or '无标题'}（{snap.uname or '?'}）" if snap and snap.uid else "解析不到房间详情",
            )
        except BiliError as exc:
            add("房间详情", False, (time.perf_counter() - start) * 1000, str(exc))

        ok = all(step["ok"] for step in steps)
        if ok:
            summary = f"这条链路通了：uid={uid}，room_id={room_id or '?'}。"
        elif any(step["name"] == "房间详情" and not step["ok"] for step in steps):
            summary = "房间能定位，但详情接口失败——多半是 B 站偶发错误或被临时限制，重试一次即可。"
        else:
            summary = "有步骤失败，看上面第一个 FAIL 项。"
        return {"ok": ok, "steps": steps, "summary": summary}

    async def aclose(self) -> None:
        await self._http.aclose()
        await self.covers.aclose()
