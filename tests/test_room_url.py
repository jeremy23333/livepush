"""直播间链接的三段回退：房间号 → 短号 → 主播空间（永不再拼成 live.bilibili.com/<UID>）。"""

from __future__ import annotations

from livepush.bili.client import ResolvedRoom
from livepush.bili.parse import RoomSnapshot
from livepush.models import LiveRoom


def test_snapshot_url_prefers_room_then_short_then_space():
    assert RoomSnapshot(uid=9, room_id=555).url == "https://live.bilibili.com/555"
    assert RoomSnapshot(uid=9, short_id=6).url == "https://live.bilibili.com/6"
    assert RoomSnapshot(uid=9).url == "https://space.bilibili.com/9"


def test_resolved_room_url_same_fallback():
    assert ResolvedRoom(uid=9, room_id=555).url == "https://live.bilibili.com/555"
    assert ResolvedRoom(uid=9, short_id=6).url == "https://live.bilibili.com/6"
    assert ResolvedRoom(uid=9).url == "https://space.bilibili.com/9"


def test_live_room_url_same_fallback():
    assert LiveRoom(uid=9, room_id=555).url == "https://live.bilibili.com/555"
    assert LiveRoom(uid=9, short_id=6).url == "https://live.bilibili.com/6"
    assert LiveRoom(uid=9).url == "https://space.bilibili.com/9"
