"""Web 层：鉴权、CRUD、设置。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(data_dir):
    from livepush.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def login(client: TestClient) -> None:
    from livepush.config import runtime

    resp = client.post("/api/login", json={"password": runtime.admin_password})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_unauthenticated_redirects(client: TestClient):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_api_requires_auth(client: TestClient):
    assert client.get("/api/overview").status_code == 401


def test_wrong_password(client: TestClient):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401


def test_login_then_overview(client: TestClient):
    login(client)
    data = client.get("/api/overview").json()
    assert data["ok"] is True
    assert data["counts"]["rooms"] == 0
    assert data["need_bot"] is True


def test_mutation_requires_csrf_header(client: TestClient):
    login(client)
    resp = client.post("/api/check")  # 缺 X-LP
    assert resp.status_code == 403


def test_bot_and_target_crud(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}
    created = client.post(
        "/api/bots",
        headers=headers,
        json={
            "name": "外接号",
            "transport": "onebot11",
            "mode": "external",
            "backend": "onebot11",
            "ws_url": "ws://127.0.0.1:3001",
        },
    )
    assert created.status_code == 200, created.text
    bot_id = created.json()["id"]

    target = client.post(
        "/api/targets",
        headers=headers,
        json={"bot_connection_id": bot_id, "kind": "group", "target_id": "123456", "name": "测试群"},
    )
    assert target.status_code == 200, target.text
    target_id = target.json()["target"]["id"]

    listed = client.get("/api/targets").json()["targets"]
    assert len(listed) == 1
    assert listed[0]["target_id"] == "123456"

    # 重复添加应 409
    dup = client.post(
        "/api/targets",
        headers=headers,
        json={"bot_connection_id": bot_id, "kind": "group", "target_id": "123456"},
    )
    assert dup.status_code == 409

    assert client.delete(f"/api/targets/{target_id}", headers=headers).status_code == 200
    assert client.get("/api/targets").json()["targets"] == []


def test_bot_create_validation(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}
    bad = client.post(
        "/api/bots",
        headers=headers,
        json={"transport": "milky", "mode": "builtin", "backend": "yogurt", "uin": 0},
    )
    assert bad.status_code == 400
    assert "QQ 号" in bad.json()["detail"]


def test_settings_roundtrip(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}
    current = client.get("/api/settings").json()["config"]
    assert current["off_confirm_polls"] == 2

    updated = client.put(
        "/api/settings",
        headers=headers,
        json={"config": {"off_confirm_polls": 3, "cover_mode": "off"}},
    ).json()["config"]
    assert updated["off_confirm_polls"] == 3
    assert updated["cover_mode"] == "off"


def test_settings_clamps_absurd_values(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}
    updated = client.put(
        "/api/settings",
        headers=headers,
        json={"config": {"poll_interval_live": 1, "chunk_size": 9999}},
    ).json()["config"]
    assert updated["poll_interval_live"] >= 10
    assert updated["chunk_size"] <= 100


def test_bindings_flow(client: TestClient):
    import sqlite3

    from livepush.config import runtime

    login(client)
    headers = {"X-LP": "1"}
    bot_id = client.post(
        "/api/bots",
        headers=headers,
        json={"transport": "onebot11", "mode": "external", "ws_url": "ws://x"},
    ).json()["id"]
    target_id = client.post(
        "/api/targets",
        headers=headers,
        json={"bot_connection_id": bot_id, "kind": "group", "target_id": "999"},
    ).json()["target"]["id"]

    # 直接写一条直播间，避免测试依赖真实 B 站接口
    conn = sqlite3.connect(runtime.settings.db_path)
    conn.execute(
        "INSERT INTO live_room (uid, room_id, short_id, uname, face, title, area_name, area_parent,"
        " cover_url, live_status, live_started_at, pending_status, pending_count, room_state, fail_streak,"
        " last_checked_at, last_ok_at, enabled, notify_on_add, note, created_at, updated_at)"
        " VALUES (7, 0, 0, '主播', '', 't', '', '', '', 0, 0, NULL, 0, 'ok', 0, 0, 0, 1, 0, '', 0, 0)"
    )
    conn.commit()
    room_id = conn.execute("SELECT id FROM live_room WHERE uid=7").fetchone()[0]
    conn.close()

    resp = client.post(
        "/api/bindings",
        headers=headers,
        json={"room_id": room_id, "target_id": target_id, "notify_live_off": False},
    )
    assert resp.status_code == 200, resp.text
    binding_id = resp.json()["id"]

    data = client.get("/api/bindings").json()
    assert len(data["bindings"]) == 1
    assert data["bindings"][0]["notify_live_off"] is False

    assert client.delete(f"/api/bindings/{binding_id}", headers=headers).status_code == 200
    assert client.get("/api/bindings").json()["bindings"] == []


def test_new_ops_endpoints_require_auth(client: TestClient):
    for path in (
        "/api/logs",
        "/api/logs/files",
        "/api/alerts/state",
        "/api/diag/bili?query=1",
        "/api/diag/network",
        "/api/hosted/backends",
    ):
        assert client.get(path).status_code == 401, path


def test_static_assets_are_versioned(client: TestClient):
    login(client)
    html = client.get("/qq").text
    assert "/static/app.js?v=" in html
    assert "/static/app.css?v=" in html
    # 版本号进 query 才能击穿 Cloudflare/浏览器的旧 JS 缓存
    assert "?v=" + html.split("/static/app.js?v=")[1].split('"')[0]


def test_logs_cleanup_requires_csrf(client: TestClient):
    login(client)
    assert client.post("/api/logs/cleanup").status_code == 403


def test_logs_endpoints_smoke(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}

    data = client.get("/api/logs?limit=10").json()
    assert data["ok"] is True
    assert isinstance(data["lines"], list)
    assert isinstance(data["areas"], list)

    files = client.get("/api/logs/files").json()
    assert files["ok"] is True
    assert isinstance(files["files"], list)

    report = client.post("/api/logs/cleanup", headers=headers).json()
    assert report["ok"] is True
    assert report["report"]["days"] >= 0


def test_alerts_state_and_test_without_config(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}

    state = client.get("/api/alerts/state").json()["state"]
    assert state["enabled"] is False

    result = client.post("/api/alerts/test", headers=headers).json()
    assert result["ok"] is False
    assert "webhook" in result["error"]


# ---------------------------------------------------------------- 添加直播间 / 托管 WebUI


class _FakeBili:
    """只实现 resolve / room_id_by_uid 的采集客户端替身。"""

    def __init__(self, room, room_id_fallback: tuple[int, int] = (0, 0)) -> None:
        self.room = room
        self.room_id_fallback = room_id_fallback

    async def resolve(self, query: str):
        return self.room

    async def room_id_by_uid(self, uid: int) -> tuple[int, int]:
        return self.room_id_fallback

    async def get_status_by_uids(self, *args, **kwargs):
        # 采集循环可能在后台 tick 一次；返回空快照即可，不碰网络
        return {}

    async def aclose(self) -> None:
        return None


def test_add_room_keeps_previewed_room_id(client: TestClient, monkeypatch):
    from livepush.bili.client import ResolvedRoom
    from livepush.state import get_engine

    login(client)
    engine = get_engine()
    monkeypatch.setattr(
        engine, "bili", _FakeBili(ResolvedRoom(uid=672328094, room_id=22637261, uname="嘉然今天吃什么"))
    )

    resp = client.post(
        "/api/rooms",
        headers={"X-LP": "1"},
        json={"query": "https://live.bilibili.com/22637261", "expected_uid": 672328094},
    )
    assert resp.status_code == 200, resp.text
    room = resp.json()["room"]
    assert room["room_id"] == 22637261
    assert room["url"] == "https://live.bilibili.com/22637261"


def test_add_room_rejects_uid_mismatch(client: TestClient, monkeypatch):
    from livepush.bili.client import ResolvedRoom
    from livepush.state import get_engine

    login(client)
    engine = get_engine()
    monkeypatch.setattr(engine, "bili", _FakeBili(ResolvedRoom(uid=99, room_id=5, uname="别人")))

    resp = client.post(
        "/api/rooms",
        headers={"X-LP": "1"},
        json={"query": "https://live.bilibili.com/42", "expected_uid": 42},
    )
    assert resp.status_code == 409
    assert "不一致" in resp.json()["detail"]


def test_add_room_falls_back_to_room_id_by_uid(client: TestClient, monkeypatch):
    from livepush.bili.client import ResolvedRoom
    from livepush.state import get_engine

    login(client)
    engine = get_engine()
    monkeypatch.setattr(
        engine, "bili", _FakeBili(ResolvedRoom(uid=42, uname="主播"), room_id_fallback=(100, 7))
    )

    resp = client.post("/api/rooms", headers={"X-LP": "1"}, json={"query": "42", "expected_uid": 42})
    assert resp.status_code == 200, resp.text
    room = resp.json()["room"]
    assert (room["room_id"], room["short_id"]) == (100, 7)
    assert room["url"] == "https://live.bilibili.com/100"


def test_hosted_page_uses_its_own_page_key(client: TestClient):
    login(client)
    resp = client.get("/hosted/1")
    assert resp.status_code == 200
    # 传 active="qq" 会让 app.js 在托管页跑 QQ 页逻辑并抛 addEventListener of null
    assert 'data-page="hosted"' in resp.text


def test_hosted_public_url_requires_auth_and_csrf(client: TestClient):
    assert client.post("/api/hosted/1/public-url", json={"url": ""}).status_code == 401
    login(client)
    assert client.post("/api/hosted/1/public-url", json={"url": ""}).status_code == 403


def test_hosted_public_url_saves(client: TestClient, monkeypatch):
    from livepush.qq.hosted import hosted_manager

    login(client)
    saved: dict[str, object] = {}

    async def fake_set(bot_id: int, url: str) -> str:
        saved["args"] = (bot_id, url)
        return "https://qq.dgnra.net/webui"

    monkeypatch.setattr(hosted_manager, "set_public_url", fake_set)

    resp = client.post(
        "/api/hosted/7/public-url",
        headers={"X-LP": "1"},
        json={"url": "https://qq.dgnra.net"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["webui_public_url"] == "https://qq.dgnra.net/webui"
    assert saved["args"] == (7, "https://qq.dgnra.net")


# ---------------------------------------------------------------- 手动通知


def _seed_room_for_notice(client: TestClient, *, with_binding: bool = True):
    import sqlite3

    from livepush.config import runtime

    headers = {"X-LP": "1"}
    bot_id = client.post(
        "/api/bots",
        headers=headers,
        json={"transport": "onebot11", "mode": "external", "ws_url": "ws://127.0.0.1:1"},
    ).json()["id"]
    target_id = client.post(
        "/api/targets",
        headers=headers,
        json={"bot_connection_id": bot_id, "kind": "group", "target_id": "123456", "name": "测试群"},
    ).json()["target"]["id"]

    conn = sqlite3.connect(runtime.settings.db_path)
    conn.execute(
        "INSERT INTO live_room (uid, room_id, short_id, uname, face, title, area_name, area_parent,"
        " cover_url, live_status, live_started_at, pending_status, pending_count, room_state, fail_streak,"
        " last_checked_at, last_ok_at, enabled, notify_on_add, note, created_at, updated_at)"
        " VALUES (99, 1234, 0, '主播', '', '标题', '', '', '', 0, 0, NULL, 0, 'ok', 0, 0, 0, 1, 0, '', 0, 0)"
    )
    conn.commit()
    room_id = conn.execute("SELECT id FROM live_room WHERE uid=99").fetchone()[0]
    conn.close()

    if with_binding:
        resp = client.post(
            "/api/bindings", headers=headers, json={"room_id": room_id, "target_id": target_id}
        )
        assert resp.status_code == 200, resp.text
    return room_id, target_id


def test_notice_endpoints_require_auth_and_csrf(client: TestClient):
    assert client.get("/api/rooms/1/notice").status_code == 401
    login(client)
    assert client.post("/api/rooms/1/notice", json={"text": "hi"}).status_code == 403


def test_notice_get_missing_room(client: TestClient):
    login(client)
    assert client.get("/api/rooms/999999/notice").status_code == 404


def test_notice_dry_run_saves_text_and_lists_targets(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}
    room_id, _ = _seed_room_for_notice(client)

    saved = client.post(
        "/api/rooms/%s/notice" % room_id,
        headers=headers,
        json={"text": "{uname} 通知 {title}", "at_all": True, "include_cover": False, "dry_run": True},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] is True
    assert saved.json()["targets"] == 1

    detail = client.get(f"/api/rooms/{room_id}/notice").json()
    assert detail["text"] == "{uname} 通知 {title}"
    assert detail["at_all"] is True
    assert detail["include_cover"] is False
    assert len(detail["targets"]) == 1
    assert detail["targets"][0]["kind_label"] == "群聊"
    assert detail["interval_sec"] > 0


def test_notice_send_without_binding_returns_400(client: TestClient):
    login(client)
    headers = {"X-LP": "1"}
    room_id, _ = _seed_room_for_notice(client, with_binding=False)

    resp = client.post("/api/rooms/%s/notice" % room_id, headers=headers, json={"text": "hi"})
    assert resp.status_code == 400
    assert "绑定" in resp.json()["detail"]

