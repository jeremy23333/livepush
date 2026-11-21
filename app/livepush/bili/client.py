"""B 站直播状态采集客户端。

只做三件事：
1. 批量查直播状态（`get_status_info_by_uids`，免登录，按 100 个 uid 分块）；
2. 把用户输入的 UID / 房间号 / 短号 / 链接解析成直播间；
3. 封面下载缓存。

风控策略：-352 降频、-412 熔断（IP 级封锁），绝不重试风暴。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .covers import DEFAULT_UA, CoverCache
from .parse import (
    SHORT_LINK_RE,
    InputRef,
    RoomSnapshot,
    parse_input,
    parse_room_get_info,
    parse_status_item,
)

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
    # 这次输入是怎么被理解的，供 UI 展示（否则用户看不出「链接被当成了 UID」）
    input_kind: str = ""  # uid / room / auto
    input_number: int = 0
    resolved_as: str = ""  # room / short / uid
    ambiguous: dict | None = None  # 同一个数字还是别的 UID 的账号时，附上对照信息

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

    @property
    def url(self) -> str:
        """直播间链接；没有房间号时退回空间链接，绝不伪造 `live/<uid>`。"""
        if self.room_id:
            return f"https://live.bilibili.com/{self.room_id}"
        if self.short_id:
            return f"https://live.bilibili.com/{self.short_id}"
        return f"https://space.bilibili.com/{self.uid}" if self.uid else ""


def _as_room_id(value: object) -> int:
    try:
        if isinstance(value, bool):
            return 0
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


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
        resolve_timeout: float = 12.0,
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
        self._resolve_budget = max(1.0, resolve_timeout)
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

    async def _get(
        self,
        path: str,
        params=None,
        *,
        stage: str = "",
        timeout: float | None = None,
        retries: int = 1,
    ) -> dict:
        """GET 一个 B 站接口。

        `timeout` 是单次请求超时（默认用客户端配置）；`retries` 是**网络层**重试次数，
        解析路径上会传 0 —— 多等一轮只会让反向代理先返回 502，掩盖真正的错误。
        """
        if self.blocked:
            raise BiliRiskControl(f"风控冷却中，剩余 {self.blocked_remaining} 秒：{self.risk_reason}")
        resp: httpx.Response | None = None
        last_exc: Exception | None = None
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            try:
                resp = await self._http.get(path, params=params, timeout=timeout)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
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
    async def _get_room_by_number(self, number: int) -> RoomSnapshot:
        """按房间号/短号查直播间。

        用 `/room/v1/Room/get_info`（实测同时接受房间号与短号，`7734200` 与 `6`
        指向同一房间）；`room_init` 已废弃——它对**合法**房间也会返回 `code=60004`，
        是「添加直播间报 502」的元凶。
        """
        stage = "room_id"
        try:
            data = await self._get(
                "/room/v1/Room/get_info",
                params={"room_id": number},
                stage=stage,
                timeout=6.0,
                retries=0,
            )
        except BiliError as exc:
            # 60004/-404/-400 是「房间不存在」；`get_info` 对不存在的房间返回 code=1
            # 「未找到该房间」，同样要当成 404，而不是让前端看到 502。
            missing = exc.code in (60004, -404, -400)
            if not missing and exc.code == 1 and ("未找到" in str(exc) or "不存在" in str(exc)):
                missing = True
            if missing:
                raise BiliNotFound(
                    f"没有找到房间号 {number} 的直播间（B 站 code={exc.code}）",
                    stage=stage,
                    code=exc.code,
                ) from exc
            raise
        snap = parse_room_get_info(data)
        if snap is None:
            raise BiliNotFound(f"房间号 {number} 没有返回有效的直播间信息", stage=stage)
        snap.room_id = snap.room_id or number
        await self._enrich(snap)
        return snap

    async def _enrich(self, snap: RoomSnapshot) -> RoomSnapshot:
        """用批量接口补齐昵称/头像/封面/分区（`get_info` 这些字段常为空）。"""
        if not snap.uid:
            return snap
        try:
            snaps = await self.get_status_by_uids([snap.uid], chunk_size=1, chunk_delay=0)
        except BiliError:
            return snap
        full = snaps.get(snap.uid)
        if full is None:
            return snap
        for attr in ("uname", "face", "cover_url", "area_name", "area_parent"):
            if not getattr(snap, attr, "") and getattr(full, attr, ""):
                setattr(snap, attr, getattr(full, attr))
        if not snap.title:
            snap.title = full.title
        if not snap.short_id:
            snap.short_id = full.short_id
        if not snap.online:
            snap.online = full.online
        if not snap.live_status and full.live_status:
            snap.live_status = full.live_status
        if not snap.live_started_at:
            snap.live_started_at = full.live_started_at
        return snap

    async def _snapshot_by_uid(self, uid: int) -> RoomSnapshot:
        """按 UID 查直播间；该 UID 没有开播房间时给明确的 404 文案。"""
        try:
            snaps = await self.get_status_by_uids([uid], chunk_size=1, chunk_delay=0)
        except BiliRiskControl:
            raise
        except BiliError as exc:
            raise BiliError(f"查询 UID {uid} 失败：{exc}", stage="status", code=exc.code) from exc
        snap = snaps.get(uid)
        if snap is None:
            info = await self.get_master_info(uid)
            who = f"（{info.get('uname')}）" if info.get("uname") else ""
            raise BiliNotFound(f"UID {uid}{who} 没有开通直播间", stage="uid")
        # 批量接口偶尔不返回 room_id（返回 0/缺字段）。这时 room_id 落库成 0，
        # 推送就会退化成 live/<uid> 的错链接——用老接口补一次，拿不到才认命。
        if not snap.room_id and not snap.short_id:
            with contextlib.suppress(BiliError):
                room_id, short_id = await self.room_id_by_uid(uid)
                snap.room_id = snap.room_id or room_id
                snap.short_id = snap.short_id or short_id
        return snap

    async def room_id_by_uid(self, uid: int) -> tuple[int, int]:
        """用免登录旧接口把 UID 换成 (room_id, short_id)。

        `getRoomInfoOld?mid=<uid>` 实测对「批量接口没给 room_id」的账号仍然可用：
        uid 672328094 → 22637261，uid 50329118 → 7734200。
        """
        data = await self._get(
            "/room/v1/Room/getRoomInfoOld",
            params={"mid": uid},
            stage="room_info_old",
            timeout=6.0,
            retries=0,
        )
        return _as_room_id(data.get("roomid") or data.get("room_id")), _as_room_id(data.get("short_id"))

    async def _other_uid_room(self, number: int, room_id: int) -> dict | None:
        """裸数字命中房间后，检查同一个数字是否还是**另一个** UP 主的 UID。"""
        try:
            snaps = await self.get_status_by_uids([number], chunk_size=1, chunk_delay=0)
        except BiliError:
            return None
        other = snaps.get(number)
        if other is None or other.room_id == room_id:
            return None
        return {
            "uid": number,
            "uname": other.uname,
            "room_id": other.room_id,
            "title": other.title,
        }

    def _finish(
        self, snap: RoomSnapshot, ref: InputRef, resolved_as: str, ambiguous: dict | None = None
    ) -> ResolvedRoom:
        room = ResolvedRoom.from_snapshot(snap)
        room.input_kind = ref.kind
        room.input_number = ref.number
        room.resolved_as = resolved_as
        room.ambiguous = ambiguous
        return room

    async def _expand_short_link(self, url: str) -> str:
        try:
            resp = await self._http.get(url, timeout=5.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise BiliError(
                f"短链解析失败：{exc}（请打开链接后复制地址栏）", stage="short_link"
            ) from exc
        return str(resp.url)

    async def _resolve_by_ref(self, ref: InputRef) -> ResolvedRoom:
        if ref.kind == "uid":
            return self._finish(await self._snapshot_by_uid(ref.number), ref, "uid")

        if ref.kind == "room":
            snap = await self._get_room_by_number(ref.number)
            resolved_as = "short" if snap.room_id and snap.room_id != ref.number else "room"
            return self._finish(snap, ref, resolved_as)

        # auto：先按房间号找，找不到再当 UID（裸数字更常见的是房间号）
        try:
            snap = await self._get_room_by_number(ref.number)
        except BiliNotFound as room_exc:
            try:
                return self._finish(await self._snapshot_by_uid(ref.number), ref, "uid")
            except BiliNotFound as uid_exc:
                raise BiliNotFound(
                    f"{room_exc}；{uid_exc}", stage="auto", code=room_exc.code
                ) from uid_exc
        resolved_as = "short" if snap.room_id and snap.room_id != ref.number else "room"
        other = await self._other_uid_room(ref.number, snap.room_id)
        return self._finish(snap, ref, resolved_as, other)

    async def get_room_info(self, room_or_short_id: int) -> RoomSnapshot:
        """[兼容保留] 按房间号/短号解析；内部已改用 `get_info`。"""
        return await self._get_room_by_number(room_or_short_id)

    async def get_master_info(self, uid: int) -> dict:
        try:
            data = await self._get("/live_user/v1/Master/info", params={"uid": uid}, stage="master_info")
        except BiliError:
            return {}
        info = data.get("info") or {}
        return {"uname": str(info.get("uname") or ""), "face": str(info.get("face") or "")}

    async def resolve(self, text: str) -> ResolvedRoom:
        """把用户输入解析成直播间。

        先给输入定型（`space.` → UID，`live.` → 房间号，裸数字 → 先房间号再 UID），
        否则 `https://live.bilibili.com/22637261` 会被当成 UID 22637261 的账号。
        整体限时 `resolve_timeout`（默认 12 秒），避免反向代理先返回 502。
        """
        raw = (text or "").strip()
        if not raw:
            raise BiliNotFound("请填写 UID、直播间号或直播间链接")
        if SHORT_LINK_RE.search(raw):
            raw = await self._expand_short_link(raw)
        ref = parse_input(raw)
        if ref is None:
            raise BiliNotFound(
                "无法从输入解析出 UID、直播间号或直播间链接"
                "（支持 space.bilibili.com/<UID>、live.bilibili.com/<房间号>、纯数字）"
            )
        try:
            return await asyncio.wait_for(self._resolve_by_ref(ref), timeout=self._resolve_budget)
        except asyncio.TimeoutError as exc:
            raise BiliError(
                f"解析超时：{self._resolve_budget:.0f} 秒内 B 站接口没有响应", stage="resolve"
            ) from exc

    async def diagnose(self, text: str) -> dict:
        """逐项体检「添加直播间」这条链路，给 Web UI / CLI 一个可读结论。"""
        steps: list[dict] = []

        def add(name: str, ok: bool, ms: float = 0.0, detail: str = "") -> None:
            steps.append({"name": name, "ok": ok, "ms": int(ms), "detail": detail})

        raw = (text or "").strip()
        if SHORT_LINK_RE.search(raw):
            try:
                raw = await self._expand_short_link(raw)
            except BiliError as exc:
                add("展开短链", False, 0, str(exc))
                return {"ok": False, "steps": steps, "summary": str(exc)}

        ref = parse_input(raw)
        add(
            "解析输入",
            ref is not None,
            0,
            f"{raw!r} → 类型={ref.kind} 数字={ref.number}（{ref.note or ref.source}）"
            if ref is not None
            else "没解析出 UID / 房间号 / 链接",
        )
        if ref is None:
            return {
                "ok": False,
                "steps": steps,
                "summary": "输入里没有可识别的 UID、直播间号或直播间链接。",
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

        room_snap: RoomSnapshot | None = None
        room_error = ""
        if ref.kind in ("room", "auto"):
            start = time.perf_counter()
            try:
                room_snap = await self._get_room_by_number(ref.number)
                add(
                    "房间接口 get_info",
                    True,
                    (time.perf_counter() - start) * 1000,
                    f"room_id={room_snap.room_id} uid={room_snap.uid} 《{room_snap.title or '无标题'}》",
                )
            except BiliNotFound as exc:
                room_error = str(exc)
                add(
                    "房间接口 get_info",
                    ref.kind == "uid",
                    (time.perf_counter() - start) * 1000,
                    str(exc),
                )
            except BiliRiskControl as exc:
                add(
                    "房间接口 get_info",
                    False,
                    (time.perf_counter() - start) * 1000,
                    f"{exc}（风控，稍后再试）",
                )
                return {"ok": False, "steps": steps, "summary": f"被 B 站风控：{exc}"}
            except BiliError as exc:
                add(
                    "房间接口 get_info",
                    False,
                    (time.perf_counter() - start) * 1000,
                    f"{exc}（stage={exc.stage or '?'}）",
                )
                return {
                    "ok": False,
                    "steps": steps,
                    "summary": "连不上 B 站接口，多半是服务器出网 / 代理 / 防火墙问题（不是房间号的问题）。",
                }

        uid_snap: RoomSnapshot | None = None
        if ref.kind in ("uid", "auto"):
            start = time.perf_counter()
            try:
                snaps = await self.get_status_by_uids([ref.number], chunk_size=1, chunk_delay=0)
                uid_snap = snaps.get(ref.number)
                add(
                    "UID 对照",
                    uid_snap is not None,
                    (time.perf_counter() - start) * 1000,
                    f"{ref.number} 作为 UID → {uid_snap.uname}（房间 {uid_snap.room_id or '?'}）"
                    if uid_snap is not None
                    else f"{ref.number} 不是开通了直播间的 UID",
                )
            except BiliError as exc:
                add("UID 对照", False, (time.perf_counter() - start) * 1000, str(exc))

        target = room_snap or uid_snap
        if target is not None and target.uid:
            start = time.perf_counter()
            try:
                snaps = await self.get_status_by_uids([target.uid], chunk_size=1, chunk_delay=0)
                got = snaps.get(target.uid)
                add(
                    "批量状态",
                    got is not None,
                    (time.perf_counter() - start) * 1000,
                    f"uid={target.uid} → {got.uname if got else '未收录（该 UID 当前没有直播间）'}",
                )
            except BiliError as exc:
                add("批量状态", False, (time.perf_counter() - start) * 1000, str(exc))

        ok = bool(room_snap or uid_snap)
        if room_snap and uid_snap and room_snap.room_id != uid_snap.room_id:
            summary = (
                f"{ref.number} 既是直播间 {room_snap.room_id}（{room_snap.uname or '?'} 的），"
                f"也是 UID {ref.number}（{uid_snap.uname or '?'}，房间 {uid_snap.room_id}）。"
                "想让哪个生效就换写法：加直播间用 live.bilibili.com/<房间号>，加 UP 主用 space.bilibili.com/<UID>。"
            )
        elif room_snap is not None:
            summary = f"识别为直播间 {room_snap.room_id}（{room_snap.uname or '?'}），这条链路通了。"
        elif uid_snap is not None:
            summary = f"识别为 UID {ref.number}（{uid_snap.uname}），直播间 {uid_snap.room_id or '?'}。"
        else:
            summary = room_error or "没有找到对应的直播间或 UID。"
        return {"ok": ok, "steps": steps, "summary": summary}

    async def aclose(self) -> None:
        await self._http.aclose()
        await self.covers.aclose()
