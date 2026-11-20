"""内嵌托管 WebUI：HTML 重写、鉴权、没有托管容器时的错误。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from livepush.web.proxy import _inject


@pytest.fixture
def client(data_dir):
    from livepush.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> None:
    from livepush.config import runtime

    assert client.post("/api/login", json={"password": runtime.admin_password}).status_code == 200


def test_inject_rewrites_absolute_attributes_once():
    html = (
        '<html><head><link rel="stylesheet" href="/static/app.css"></head>'
        '<body><img src="/files/a.png"><a href="https://x.test/y">external</a>'
        '<a href="//cdn.test/z">proto-relative</a></body></html>'
    )
    out = _inject(html, "/hosted/7/webui")

    assert '<base href="/hosted/7/webui/">' in out
    assert 'href="/hosted/7/webui/static/app.css"' in out
    assert 'src="/hosted/7/webui/files/a.png"' in out
    # 外链与协议相对链接不能被动
    assert 'href="https://x.test/y"' in out
    assert 'href="//cdn.test/z"' in out
    # base 自己不能被二次改写
    assert '/hosted/7/webui//hosted/7/webui/' not in out
    assert "window.fetch" in out


def test_inject_handles_fragment_without_head():
    out = _inject("<div>hi</div>", "/hosted/1/webui")
    assert '<base href="/hosted/1/webui/">' in out
    assert out.endswith("<div>hi</div>")


def test_proxy_requires_login(client: TestClient):
    assert client.get("/hosted/1/webui/").status_code == 401


def test_proxy_page_requires_login(client: TestClient):
    resp = client.get("/hosted/1", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_proxy_404_without_hosted_engine(client: TestClient):
    _login(client)
    resp = client.get("/hosted/999/webui/")
    assert resp.status_code == 404
    assert "托管" in resp.json()["detail"]


def test_proxy_page_renders_iframe(client: TestClient):
    _login(client)
    resp = client.get("/hosted/5")
    assert resp.status_code == 200
    assert '/hosted/5/webui/' in resp.text
    assert "<iframe" in resp.text
