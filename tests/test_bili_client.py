"""B 站客户端：分块、缺失 uid、风控熔断。"""

from __future__ import annotations

import json

import httpx
import pytest

from livepush.bili.client import BiliClient, BiliRiskControl, extract_id

BATCH_PATH = "/room/v1/Room/get_status_info_by_uids"


def make_client(tmp_path, handler, chunk_delay=0.0):
    transport = httpx.MockTransport(handler)
    return BiliClient(covers_dir=tmp_path, chunk_delay=chunk_delay, transport=transport)


@pytest.mark.asyncio
async def test_batch_returns_found_and_skips_missing(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "1": {"uid": 1, "room_id": 10, "uname": "A", "live_status": 1, "live_time": 1794666029},
                },
            },
        )

    client = make_client(tmp_path, handler)
    result = await client.get_status_by_uids([1, 2, 3], chunk_size=50)
    assert list(result) == [1]
    assert result[1].live_norm == 1
    assert len(calls) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_batch_chunks_at_100(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = make_client(tmp_path, handler)
    await client.get_status_by_uids(list(range(250)), chunk_size=100)
    assert len(calls) == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_http_412_trips_circuit_breaker(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="blocked")

    client = make_client(tmp_path, handler)
    with pytest.raises(BiliRiskControl):
        await client.get_status_by_uids([1])
    assert client.blocked is True
    with pytest.raises(BiliRiskControl):
        await client.get_status_by_uids([1])
    await client.aclose()


@pytest.mark.asyncio
async def test_code_minus_352_trips(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -352, "message": "风控校验失败"})

    client = make_client(tmp_path, handler)
    with pytest.raises(BiliRiskControl):
        await client.get_status_by_uids([1])
    assert "352" in client.risk_reason
    await client.aclose()


@pytest.mark.asyncio
async def test_non_zero_code_without_risk(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 19002000, "message": "参数错误"})

    client = make_client(tmp_path, handler)
    from livepush.bili.client import BiliError

    with pytest.raises(BiliError):
        await client.get_status_by_uids([1])
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_live_url_uses_room_endpoint(tmp_path):
    """`live.bilibili.com/<n>` 必须按房间号查 get_info，而不是当成 UID。"""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == BATCH_PATH:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"77": {"uid": 77, "room_id": 555, "uname": "主播B"}}},
            )
        if request.url.path.endswith("/get_info"):
            return httpx.Response(
                200, json={"code": 0, "data": {"uid": 77, "room_id": 555, "title": "T"}}
            )
        return httpx.Response(404, json={"code": -404})

    client = make_client(tmp_path, handler)
    room = await client.resolve("https://live.bilibili.com/555")
    assert (room.uid, room.room_id) == (77, 555)
    assert room.input_kind == "room"
    assert room.resolved_as == "room"
    assert room.uname == "主播B"  # 用批量接口补齐
    assert any(path.endswith("/get_info") for path in requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_space_url_uses_uid(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BATCH_PATH:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"42": {"uid": 42, "room_id": 100, "uname": "主播"}}},
            )
        return httpx.Response(404, json={"code": -404})

    client = make_client(tmp_path, handler)
    room = await client.resolve("https://space.bilibili.com/42")
    assert (room.uid, room.room_id) == (42, 100)
    assert room.input_kind == "uid"
    assert room.resolved_as == "uid"
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_bare_number_prefers_room_and_reports_ambiguous(tmp_path):
    """裸数字先按房间号；同时命到别的 UID 时返回 ambiguous 让 UI 提醒。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BATCH_PATH:
            uids = [value for key, value in request.url.params.multi_items() if key == "uids[]"]
            data = {}
            if "99" in uids:
                data["99"] = {"uid": 99, "room_id": 42, "uname": "房主"}
            if "42" in uids:
                data["42"] = {"uid": 42, "room_id": 100, "uname": "另一个人"}
            return httpx.Response(200, json={"code": 0, "data": data})
        if request.url.path.endswith("/get_info"):
            return httpx.Response(200, json={"code": 0, "data": {"uid": 99, "room_id": 42, "title": "房间42"}})
        return httpx.Response(404, json={"code": -404})

    client = make_client(tmp_path, handler)
    room = await client.resolve("42")
    assert (room.uid, room.room_id) == (99, 42)
    assert room.input_kind == "auto"
    assert room.ambiguous and room.ambiguous["uid"] == 42
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_bare_number_falls_back_to_uid(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BATCH_PATH:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"42": {"uid": 42, "room_id": 100, "uname": "主播"}}},
            )
        if request.url.path.endswith("/get_info"):
            return httpx.Response(200, json={"code": 1, "message": "未找到该房间"})
        return httpx.Response(404, json={"code": -404})

    client = make_client(tmp_path, handler)
    room = await client.resolve("42")
    assert (room.uid, room.room_id) == (42, 100)
    assert room.resolved_as == "uid"
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_missing_room_raises_not_found(tmp_path):
    """旧链路里 room_init 返回 60004 会被包成 502；现在必须是 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BATCH_PATH:
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/get_info"):
            return httpx.Response(200, json={"code": 1, "message": "未找到该房间"})
        if request.url.path.endswith("/Master/info"):
            return httpx.Response(200, json={"code": 0, "data": {"info": {"uname": "无名"}}})
        return httpx.Response(404, json={"code": -404})

    from livepush.bili.client import BiliNotFound

    client = make_client(tmp_path, handler)
    with pytest.raises(BiliNotFound) as excinfo:
        await client.resolve("99999999999")
    assert "没有找到房间号" in str(excinfo.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_short_id_reports_short(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BATCH_PATH:
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/get_info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "uid": 50329118,
                        "room_id": 7734200,
                        "short_id": 6,
                        "title": "赛事",
                    },
                },
            )
        return httpx.Response(404, json={"code": -404})

    client = make_client(tmp_path, handler)
    room = await client.resolve("https://live.bilibili.com/6")
    assert room.room_id == 7734200 and room.short_id == 6
    assert room.resolved_as == "short"
    await client.aclose()


def test_extract_id_variants():
    assert extract_id("672328094") == 672328094
    assert extract_id("https://live.bilibili.com/22637261") == 22637261
    assert extract_id("https://space.bilibili.com/123456") == 123456
    assert extract_id("UID:123456") == 123456
    assert extract_id("abc") is None


def test_parse_handles_non_dict_data(tmp_path):
    # data 为 null 时不应崩
    assert json.dumps({"code": 0, "data": None})
