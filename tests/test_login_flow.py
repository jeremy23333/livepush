"""扫码登录状态机（用桩进程，不真的拉起协议端）。"""

from __future__ import annotations

import pytest

from livepush.models import now_ts
from livepush.qq.backends import YOGURT, LaunchContext
from livepush.qq.login import QR_TTL, REUSE_GRACE, LoginFlow


class StubOutput:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def tail(self, n: int = 200) -> list[str]:
        return self._lines[-n:]

    def search(self, markers, window: int = 200):
        for line in reversed(self._lines[-window:]):
            for marker in markers:
                if marker and marker in line:
                    return marker
        return None


class StubProcess:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.output = StubOutput(lines or [])
        self.running = True
        self.exit_code = None
        self.pid = 4242
        self.total_seconds = 0

    def tail_text(self, n: int = 80) -> str:
        return "\n".join(self.output._lines[-n:])

    async def stop(self) -> None:
        self.running = False

    async def start(self) -> None:
        self.running = True


def make_flow(tmp_path, lines=None, max_attempts: int = 0) -> LoginFlow:
    flow = LoginFlow(
        conn_id=1,
        uin=12345,
        spec=YOGURT,
        workdir=tmp_path,
        ctx=LaunchContext(uin=12345, http_port=3000, access_token="t"),
        log_path=tmp_path / "proto.log",
        max_attempts=max_attempts,
    )
    flow.process = StubProcess(lines)
    flow.state = "launching"
    return flow


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.mark.asyncio
async def test_poll_detects_qr_file(tmp_path):
    (tmp_path / "qrcode.png").write_bytes(PNG)
    flow = make_flow(tmp_path, ["starting yogurt...", "二维码文件已保存"])
    await flow.poll()
    assert flow.state == "qr_ready"
    assert flow.qr is not None
    assert flow.qr.kind == "file"
    snap = flow.snapshot()
    assert snap["qr"]["data_uri"].startswith("data:image/png;base64,")
    assert snap["state"] == "qr_ready"


@pytest.mark.asyncio
async def test_poll_keystore_only_enters_reusing_session(tmp_path):
    """旧登录态存在不等于还能用：先进入验证态，再由 get_login_info 决断。"""
    (tmp_path / "session-store.json").write_text("{}", encoding="utf-8")
    flow = make_flow(tmp_path, ["launching"])
    await flow.poll()
    assert flow.state == "reusing_session"
    assert flow.snapshot()["done"] is False


@pytest.mark.asyncio
async def test_reusing_session_confirmed_becomes_logged_in(tmp_path, monkeypatch):
    flow = make_flow(tmp_path, ["launching"])
    flow.state = "reusing_session"
    flow.reuse_at = now_ts()

    async def ok() -> bool:
        return True

    monkeypatch.setattr(flow, "_confirm_session", ok)
    await flow.poll()
    assert flow.state == "logged_in"
    assert flow.snapshot()["done"] is True


@pytest.mark.asyncio
async def test_reusing_session_falls_back_to_qr_after_grace(tmp_path, monkeypatch):
    flow = make_flow(tmp_path, ["launching"])
    flow.state = "reusing_session"
    flow.reuse_at = now_ts() - REUSE_GRACE - 1

    async def bad() -> bool:
        return False

    monkeypatch.setattr(flow, "_confirm_session", bad)
    await flow.poll()
    assert flow.state == "launching"
    assert flow.reuse_failed is True


@pytest.mark.asyncio
async def test_poll_detects_login_by_marker(tmp_path):
    flow = make_flow(tmp_path, ["已上线，账号：12345"])
    await flow.poll()
    assert flow.state == "logged_in"


@pytest.mark.asyncio
async def test_poll_marks_error_when_process_exits(tmp_path):
    flow = make_flow(tmp_path, [])
    flow.process.running = False
    flow.process.exit_code = 1
    await flow.poll()
    assert flow.state == "error"
    assert "退出" in flow.error


@pytest.mark.asyncio
async def test_poll_expired_qr_stops_after_attempt_budget(tmp_path):
    (tmp_path / "qrcode.png").write_bytes(PNG)
    flow = make_flow(tmp_path, ["二维码已过期"], max_attempts=0)
    flow.attempts = 1
    await flow.poll()
    assert flow.state == "error"
    assert "连续" in flow.error


@pytest.mark.asyncio
async def test_poll_reports_code_45(tmp_path):
    flow = make_flow(tmp_path, ["login failed: code=45"])
    await flow.poll()
    assert flow.state == "error"
    assert "签名" in flow.error


@pytest.mark.asyncio
async def test_qr_expires_after_ttl(tmp_path):
    (tmp_path / "qrcode.png").write_bytes(PNG)
    flow = make_flow(tmp_path, ["二维码文件已保存"], max_attempts=0)
    await flow.poll()
    assert flow.state == "qr_ready"
    # 把二维码时间往前拨到过期
    flow.qr_at -= QR_TTL + 5
    flow.attempts = 1
    await flow.poll()
    assert flow.state == "error"


@pytest.mark.asyncio
async def test_launch_without_binary_raises_clear_error(tmp_path):
    from livepush.qq.backends import YOGURT as SPEC

    flow = LoginFlow(
        conn_id=2,
        uin=1,
        spec=SPEC,
        workdir=tmp_path / "wd",
        ctx=LaunchContext(uin=1),
        log_path=tmp_path / "x.log",
    )
    flow.process = None
    with pytest.raises(FileNotFoundError) as exc:
        flow._build_process()
    assert "找不到协议端可执行文件" in str(exc.value)
