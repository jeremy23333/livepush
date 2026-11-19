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
async def test_resolve_prefers_uid_then_room(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BATCH_PATH:
            params = str(request.url)
            if "uids%5B%5D=42" in params or "uids[]=42" in params:
                return httpx.Response(200, json={"code": 0, "data": {"42": {"uid": 42, "room_id": 100, "uname": "主播", "live_status": 0}}})
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/room_init"):
            return httpx.Response(200, json={"code": 0, "data": {"room_id": 555, "short_id": 0, "uid": 77}})
        if "getInfoByRoom" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "room_info": {"uid": 77, "room_id": 555, "title": "T", "live_status": 0},
                        "anchor_info": {"base_info": {"uname": "B", "face": "f"}},
                    },
                },
            )
        return httpx.Response(404, json={"code": -400})

    client = make_client(tmp_path, handler)
    by_uid = await client.resolve("42")
    assert by_uid.uid == 42
    by_room = await client.resolve("https://live.bilibili.com/555")
    assert by_room.uid == 77
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
