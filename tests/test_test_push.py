"""绑定级「测试推送」：用真实数据渲染、末尾标注测试、绝不 @全体。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from livepush.qq import registry
from livepush.qq.base import SendResult


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个用例都从空限流表开始（不同用例的数据库里绑定 id 可能相同）。"""
    from livepush.web import routes_api

    routes_api._BINDING_TEST_AT.clear()
    yield
    routes_api._BINDING_TEST_AT.clear()


@pytest.fixture
def client(data_dir):
    from livepush.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> None:
    from livepush.config import runtime

    assert client.post("/api/login", json={"password": runtime.admin_password}).status_code == 200


def _seed(client: TestClient) -> tuple[int, int]:
    """建一条外接 OneBot 连接 + 目标 + 直播间 + 绑定，返回 (binding_id, room_id)。"""
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
        " VALUES (42, 98765, 0, '测试主播', '', '测试标题', '手游', '鸣潮',"
        " '', 1, 0, NULL, 0, 'ok', 0, 0, 0, 1, 0, '', 0, 0)"
    )
    conn.commit()
    room_id = conn.execute("SELECT id FROM live_room WHERE uid=42").fetchone()[0]
    conn.close()

    binding = client.post(
        "/api/bindings",
        headers=headers,
        json={"room_id": room_id, "target_id": target_id, "include_cover": False},
    ).json()
    return binding["id"], room_id


class FakeClient:
    connected = True
    state = "connected"
    self_id = 1
    nickname = "测试号"
    last_error = ""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(self, kind: str, target_id: str, message) -> SendResult:
        self.sent.append((kind, target_id, message.preview()))
        return SendResult.success()


def test_test_push_renders_real_data_with_marker(client: TestClient, monkeypatch):
    _login(client)
    binding_id, _ = _seed(client)

    fake = FakeClient()

    async def fake_get(conn, autostart: bool = True):
        return fake

    monkeypatch.setattr(registry.manager, "get", fake_get)

    resp = client.post(
        f"/api/subscriptions/{binding_id}/test-push",
        headers={"X-LP": "1"},
        json={"kind": "live_on"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True

    assert len(fake.sent) == 1
    kind, target_id, text = fake.sent[0]
    assert kind == "group"
    assert target_id == "123456"
    assert "测试主播" in text
    assert "开播" in text
    assert "测试消息" in text
    assert "不是真实的开播/下播" in text
    # 测试消息不应该 @全体
    assert "mention_all" not in text and "全体" not in text
    assert "test" in data["preview"] or "测试" in data["preview"]


def test_test_push_offline_bot_returns_503(client: TestClient, monkeypatch):
    _login(client)
    binding_id, _ = _seed(client)

    class OfflineClient(FakeClient):
        connected = False
        state = "stopped"

    async def fake_get(conn, autostart: bool = True):
        return OfflineClient()

    monkeypatch.setattr(registry.manager, "get", fake_get)

    resp = client.post(
        f"/api/subscriptions/{binding_id}/test-push",
        headers={"X-LP": "1"},
        json={"kind": "live_off"},
    )
    assert resp.status_code == 503
    assert "未连接" in resp.json()["detail"]


def test_test_push_missing_binding(client: TestClient):
    _login(client)
    resp = client.post(
        "/api/subscriptions/99999/test-push",
        headers={"X-LP": "1"},
        json={"kind": "live_on"},
    )
    assert resp.status_code == 404


def test_test_push_requires_csrf(client: TestClient):
    _login(client)
    assert client.post("/api/subscriptions/1/test-push", json={}).status_code == 403
