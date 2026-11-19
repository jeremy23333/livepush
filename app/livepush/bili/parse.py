"""B 站接口的数据解析（纯函数，便于单测）。"""

from __future__ import annotations

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
        return f"https://live.bilibili.com/{self.display_room_id}"


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
    """`room_init` 返回 (room_id, short_id, uid)。"""
    if not isinstance(data, dict):
        return 0, 0, 0
    return _as_int(data.get("room_id")), _as_int(data.get("short_id")), _as_int(data.get("uid"))
