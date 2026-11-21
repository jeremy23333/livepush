"""B 站接口的数据解析（纯函数，便于单测）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from ..util.timefmt import TZ


def normalize_live_time(value: object) -> int:
    """`live_time` 实测是 unix 秒，但文档写的是 'YYYY-MM-DD HH:mm:ss'，两种都兼容。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text.isdigit():
            return int(text)
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return int(datetime.strptime(text, pattern).replace(tzinfo=TZ).timestamp())
            except ValueError:
                continue
    return 0


def _as_int(value: object, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


@dataclass(slots=True)
class RoomSnapshot:
    """一次采集拿到的直播间快照。"""

    uid: int
    room_id: int = 0
    short_id: int = 0
    uname: str = ""
    face: str = ""
    title: str = ""
    area_name: str = ""
    area_parent: str = ""
    cover_url: str = ""
    live_status: int = 0
    live_started_at: int = 0
    online: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def live_norm(self) -> int:
        """归一化开播状态：轮播(2) 视为未开播。"""
        return 1 if self.live_status == 1 else 0

    @property
    def area(self) -> str:
        if self.area_parent and self.area_name:
            return f"{self.area_parent} / {self.area_name}"
        return self.area_name or self.area_parent or "未知分区"

    @property
    def display_room_id(self) -> int:
        return self.room_id or self.short_id or self.uid

    @property
    def url(self) -> str:
        """直播间链接；**没有房间号时宁可用空间链接，也不要伪造 live/<uid>**。"""
        if self.room_id:
            return f"https://live.bilibili.com/{self.room_id}"
        if self.short_id:
            return f"https://live.bilibili.com/{self.short_id}"
        return f"https://space.bilibili.com/{self.uid}" if self.uid else ""


def parse_status_item(uid: int, raw: dict) -> RoomSnapshot | None:
    """解析 `get_status_info_by_uids` 里单个 uid 的对象。"""
    if not isinstance(raw, dict):
        return None
    cover = _as_str(raw.get("cover_from_user")) or _as_str(raw.get("keyframe")) or _as_str(raw.get("user_cover"))
    return RoomSnapshot(
        uid=_as_int(raw.get("uid"), uid) or uid,
        room_id=_as_int(raw.get("room_id")),
        short_id=_as_int(raw.get("short_id")),
        uname=_as_str(raw.get("uname")),
        face=_as_str(raw.get("face")),
        title=_as_str(raw.get("title")),
        area_name=_as_str(raw.get("area_v2_name")) or _as_str(raw.get("area_name")),
        area_parent=_as_str(raw.get("area_v2_parent_name")),
        cover_url=cover,
        live_status=_as_int(raw.get("live_status")),
        live_started_at=normalize_live_time(raw.get("live_time")),
        online=_as_int(raw.get("online")),
        raw=raw,
    )


def parse_room_info_payload(payload: dict) -> RoomSnapshot | None:
    """解析 `getInfoByRoom` 的返回（用于按房间号解析）。"""
    if not isinstance(payload, dict):
        return None
    info = payload.get("room_info") or {}
    anchor = ((payload.get("anchor_info") or {}).get("base_info")) or {}
    if not info:
        return None
    cover = _as_str(info.get("cover")) or _as_str(info.get("keyframe"))
    return RoomSnapshot(
        uid=_as_int(info.get("uid")),
        room_id=_as_int(info.get("room_id")),
        short_id=_as_int(info.get("short_id")),
        uname=_as_str(anchor.get("uname")),
        face=_as_str(anchor.get("face")),
        title=_as_str(info.get("title")),
        area_name=_as_str(info.get("area_name")),
        area_parent=_as_str(info.get("parent_area_name")),
        cover_url=cover,
        live_status=_as_int(info.get("live_status")),
        live_started_at=normalize_live_time(info.get("live_start_time")),
        online=_as_int(info.get("online")),
        raw=payload,
    )


def parse_room_init(data: dict) -> tuple[int, int, int]:
    """`room_init` 返回 (room_id, short_id, uid)。

    注意：实测该接口对**合法**房间也可能返回 `code=60004`，B 站主链路已改用
    `parse_room_get_info`（`/room/v1/Room/get_info`），这里只留给旧调用与测试。
    """
    if not isinstance(data, dict):
        return 0, 0, 0
    return _as_int(data.get("room_id")), _as_int(data.get("short_id")), _as_int(data.get("uid"))


def parse_room_get_info(data: dict) -> RoomSnapshot | None:
    """解析 `/room/v1/Room/get_info` 的返回。

    该接口实测**同时接受房间号与短号**（`room_id=7734200` 与 `room_id=6` 指向同一房间），
    且免登录可用，是「按房间号添加直播间」的主链路。
    """
    if not isinstance(data, dict):
        return None
    room_id = _as_int(data.get("room_id"))
    uid = _as_int(data.get("uid"))
    if not room_id and not uid:
        return None
    cover = _as_str(data.get("user_cover")) or _as_str(data.get("cover")) or _as_str(data.get("keyframe"))
    return RoomSnapshot(
        uid=uid,
        room_id=room_id,
        short_id=_as_int(data.get("short_id")),
        uname=_as_str(data.get("uname")),
        face=_as_str(data.get("face")),
        title=_as_str(data.get("title")),
        area_name=_as_str(data.get("area_name")),
        area_parent=_as_str(data.get("parent_area_name")),
        cover_url=cover,
        live_status=_as_int(data.get("live_status")),
        live_started_at=normalize_live_time(data.get("live_time")),
        online=_as_int(data.get("online")),
        raw=data,
    )


# --------------------------------------------------------------------- 输入定型
@dataclass(slots=True)
class InputRef:
    """用户输入定型结果。

    B 站允许「房间号」与「UID」是同一个数字（例如 22637261 既是某个直播间，
    也是另一个 UP 主的 UID），所以必须先看清输入形态再决定查哪个接口：
    `space.bilibili.com/<n>` 只能是 UID，`live.bilibili.com/<n>` 只能是房间号，
    裸数字才需要「先房间号、再 UID」的兜底。
    """

    kind: str  # "uid" | "room" | "auto"
    number: int
    source: str  # 命中的规则，便于 UI / 诊断展示
    note: str = ""


_SPACE_RE = re.compile(r"space\.bilibili\.com/(?:#/)?(\d+)", re.I)
_ROOM_PARAM_RE = re.compile(r"live\.bilibili\.com/[^\s]*?[?&]room_id=(\d+)", re.I)
_LIVE_PATH_RE = re.compile(r"live\.bilibili\.com/(?:h5/|blanc/|blackboard/)?(\d+)", re.I)
_UID_PREFIX_RE = re.compile(r"(?:uid|用户\s*id|用户\s*号)\s*[:=：]?\s*(\d+)", re.I)
_ROOM_PREFIX_RE = re.compile(r"(?:房间号|直播间号|短号|room(?:_?id)?)\s*[:=：]?\s*(\d+)", re.I)
_BARE_RE = re.compile(r"^(\d+)$")
SHORT_LINK_RE = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/\S+", re.I)


def parse_input(text: str) -> InputRef | None:
    """把用户输入定成 `uid` / `room` / `auto` 三类之一；无法识别返回 None。"""
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    if match := _ROOM_PARAM_RE.search(raw):
        return InputRef("room", int(match.group(1)), "room_id 参数", "链接里带 room_id，按直播间号解析")
    if match := _LIVE_PATH_RE.search(raw):
        return InputRef("room", int(match.group(1)), "live.bilibili.com", "直播间链接，按直播间号解析")
    if match := _SPACE_RE.search(raw):
        return InputRef("uid", int(match.group(1)), "space.bilibili.com", "个人空间链接，按 UID 解析")
    if match := _UID_PREFIX_RE.search(raw):
        return InputRef("uid", int(match.group(1)), "uid 前缀", "带 uid 标记，按 UID 解析")
    if match := _ROOM_PREFIX_RE.search(raw):
        return InputRef("room", int(match.group(1)), "房间号前缀", "带房间号标记，按直播间号解析")
    if match := _BARE_RE.match(raw):
        return InputRef("auto", int(match.group(1)), "纯数字", "纯数字：先按直播间号找，找不到再当 UID")
    return None
