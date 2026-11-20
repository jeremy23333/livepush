"""输入定型：UID / 直播间号 / 链接必须区分开。

B 站允许「房间号」与「UID」是同一个数字（22637261 既是一个直播间，也是
另一个 UP 主的 UID），所以链接形态决定了查哪个接口。
"""

from __future__ import annotations

import pytest

from livepush.bili.parse import SHORT_LINK_RE, parse_input, parse_room_get_info


def test_space_url_is_uid():
    ref = parse_input("https://space.bilibili.com/672328094")
    assert ref is not None
    assert (ref.kind, ref.number) == ("uid", 672328094)


def test_space_url_with_hash_route():
    ref = parse_input("https://space.bilibili.com/672328094/dynamic")
    assert ref is not None
    assert (ref.kind, ref.number) == ("uid", 672328094)


def test_live_url_is_room():
    ref = parse_input("https://live.bilibili.com/22637261")
    assert ref is not None
    assert (ref.kind, ref.number) == ("room", 22637261)


def test_live_url_with_query_and_h5_path():
    assert parse_input("https://live.bilibili.com/22637261?broadcast_type=0") == parse_input(
        "https://live.bilibili.com/22637261"
    )
    ref = parse_input("https://live.bilibili.com/h5/22637261")
    assert ref is not None
    assert (ref.kind, ref.number) == ("room", 22637261)


def test_room_id_query_param_wins():
    ref = parse_input("https://live.bilibili.com/whatever?room_id=7734200&x=1")
    assert ref is not None
    assert (ref.kind, ref.number) == ("room", 7734200)


def test_bare_number_is_auto():
    ref = parse_input("22637261")
    assert ref is not None
    assert (ref.kind, ref.number) == ("auto", 22637261)


def test_prefixed_forms():
    user = parse_input("UID:123456")
    assert user is not None and user.kind == "uid" and user.number == 123456
    room = parse_input("房间号 7734200")
    assert room is not None and room.kind == "room" and room.number == 7734200


def test_unknown_text_returns_none():
    assert parse_input("abc") is None
    assert parse_input("") is None
    assert parse_input("   ") is None


def test_short_link_pattern():
    assert SHORT_LINK_RE.search("https://b23.tv/abcdEFG")
    assert SHORT_LINK_RE.search("https://bili2233.cn/xyz")
    assert not SHORT_LINK_RE.search("https://live.bilibili.com/6")


@pytest.mark.parametrize(
    "payload",
    [
        {"uid": 1, "room_id": 2, "title": "T", "live_status": 1},
        {"uid": 1, "room_id": 2, "live_time": "2026-11-14 10:25:51"},
    ],
)
def test_parse_room_get_info_basic(payload):
    snap = parse_room_get_info(payload)
    assert snap is not None
    assert snap.uid == 1 and snap.room_id == 2


def test_parse_room_get_info_rejects_junk():
    assert parse_room_get_info({}) is None
    assert parse_room_get_info({"code": 1}) is None
