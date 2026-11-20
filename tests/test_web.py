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
        "/api/hosted/backends",
    ):
        assert client.get(path).status_code == 401, path


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
