"""B 站返回解析测试。"""

from __future__ import annotations

from livepush.bili.parse import normalize_live_time, parse_room_info_payload, parse_room_init, parse_status_item


def test_normalize_live_time_unix_seconds():
    assert normalize_live_time(1794666029) == 1794666029


def test_normalize_live_time_string_datetime():
    value = normalize_live_time("2026-11-14 23:20:00")
    assert value > 0
    assert normalize_live_time("2026-11-14 23:20") > 0


def test_normalize_live_time_edge_values():
    assert normalize_live_time(0) == 0
    assert normalize_live_time("0") == 0
    assert normalize_live_time("") == 0
    assert normalize_live_time(None) == 0
    assert normalize_live_time(True) == 0
    assert normalize_live_time(-5) == 0


def test_parse_status_item_maps_cover_fallback():
    raw = {
        "uid": 42,
        "room_id": 100,
        "short_id": 0,
        "uname": "主播",
        "title": "标题",
        "live_status": 1,
        "live_time": 1794666029,
        "cover_from_user": "",
        "keyframe": "https://i0.hdslb.com/key.jpg",
        "area_v2_name": "日常",
        "area_v2_parent_name": "生活",
        "online": 1234,
    }
    snap = parse_status_item(42, raw)
    assert snap is not None
    assert snap.uid == 42
    assert snap.cover_url.endswith("key.jpg")
    assert snap.live_norm == 1
    assert snap.area == "生活 / 日常"
    assert snap.url.endswith("/100")


def test_parse_status_item_carousel_is_offline():
    snap = parse_status_item(7, {"uid": 7, "live_status": 2})
    assert snap is not None
    assert snap.live_norm == 0
    assert snap.live_status == 2


def test_parse_room_info_payload():
    payload = {
        "room_info": {
            "uid": 9,
            "room_id": 200,
            "short_id": 0,
            "title": "标题",
            "live_status": 0,
            "live_start_time": 0,
            "area_name": "分区",
            "parent_area_name": "父分区",
            "cover": "https://i0.hdslb.com/cover.jpg",
        },
        "anchor_info": {"base_info": {"uname": "主播", "face": "https://i0.hdslb.com/face.jpg"}},
    }
    snap = parse_room_info_payload(payload)
    assert snap is not None
    assert snap.uname == "主播"
    assert snap.area == "父分区 / 分区"
    assert snap.display_room_id == 200


def test_parse_room_init():
    assert parse_room_init({"room_id": 100, "short_id": 5, "uid": 42}) == (100, 5, 42)
    assert parse_room_init({}) == (0, 0, 0)
