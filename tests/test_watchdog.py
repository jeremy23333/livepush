"""QQ 掉线看门狗：组合判据、宽限期、托管容器状态。"""

from __future__ import annotations

import logging

import pytest

from livepush.config import Runtime
from livepush.db import init_engine, init_schema, session_scope
from livepush.models import BotConnection, HostedEngine
from livepush.qq import registry
from livepush.qq.watchdog import BotWatchdog


class FakeClient:
    def __init__(
        self,
        *,
        connected: bool = True,
        health: dict | None = None,
        login_ok: bool = True,
    ) -> None:
        self.state = "connected" if connected else "disconnected"
        self.connected = connected
        self.self_id = 10001
        self.nickname = "测试号"
        self.last_error = ""
        self._health = health or {}
        self._login_ok = login_ok

    def health(self) -> dict:
        return dict(self._health)

    async def get_login_info(self) -> dict:
        if not self._login_ok:
            raise RuntimeError("timed out")
        return {"self_id": self.self_id, "nickname": self.nickname}


async def _setup(tmp_path, monkeypatch) -> BotConnection:
    monkeypatch.setenv("LIVEPUSH_DATA_DIR", str(tmp_path))
    Runtime.load()
    await init_engine(Runtime.settings.db_url)
    await init_schema()
    async with session_scope() as session:
        bot = BotConnection(name="小号", transport="onebot11", mode="external", ws_url="ws://127.0.0.1:1")
        session.add(bot)
        await session.flush()
        return bot


@pytest.mark.asyncio
async def test_healthy_client_has_no_issue(tmp_path, monkeypatch):
    bot = await _setup(tmp_path, monkeypatch)
    client = FakeClient(health={"heartbeat_good": True, "heartbeat_age": 3, "heartbeat_interval": 30000})
    monkeypatch.setattr(registry.manager, "peek", lambda bot_id: client)

    watchdog = BotWatchdog()
    await watchdog._check(bot, None, interval=30, grace=0, threshold=2)

    status = watchdog.status(bot.id)
    assert status["healthy"] is True
    assert status["issues"] == []
    assert status["login_probe_ok"] is True


@pytest.mark.asyncio
async def test_kicked_offline_is_reported(tmp_path, monkeypatch, caplog):
    bot = await _setup(tmp_path, monkeypatch)
    client = FakeClient(
        health={
            "heartbeat_good": False,
            "heartbeat_age": 500,
            "heartbeat_interval": 30000,
            "last_notice": {"notice_type": "bot_status", "sub_type": "offline"},
        },
        login_ok=False,
    )
    monkeypatch.setattr(registry.manager, "peek", lambda bot_id: client)

    watchdog = BotWatchdog()
    with caplog.at_level(logging.WARNING, logger="livepush.qq.watchdog"):
        await watchdog._check(bot, None, interval=30, grace=0, threshold=1)

    status = watchdog.status(bot.id)
    assert status["healthy"] is False
    joined = "；".join(status["issues"])
    assert "get_login_info 失败" in joined
    assert "心跳" in joined
    assert "下线通知" in joined
    assert any("疑似掉线" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_grace_period_delays_alert(tmp_path, monkeypatch, caplog):
    bot = await _setup(tmp_path, monkeypatch)
    client = FakeClient(connected=False, health={})
    monkeypatch.setattr(registry.manager, "peek", lambda bot_id: client)

    watchdog = BotWatchdog()
    with caplog.at_level(logging.WARNING, logger="livepush.qq.watchdog"):
        await watchdog._check(bot, None, interval=30, grace=3600, threshold=2)

    status = watchdog.status(bot.id)
    assert status["healthy"] is False
    assert status["suspect_seconds"] >= 0
    assert not any("疑似掉线" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_container_exited_is_reported(tmp_path, monkeypatch):
    bot = await _setup(tmp_path, monkeypatch)

    class FakeDocker:
        async def state(self, name: str) -> str:
            return "exited"

        async def logs(self, name: str, tail: int = 60) -> str:  # pragma: no cover
            return ""

    client = FakeClient(health={"heartbeat_good": True, "heartbeat_age": 2})
    monkeypatch.setattr(registry.manager, "peek", lambda bot_id: client)

    async with session_scope() as session:
        engine = HostedEngine(bot_connection_id=bot.id, backend="napcat", container_name="livepush-napcat-1")
        session.add(engine)

    watchdog = BotWatchdog()
    from livepush.qq.hosted import hosted_manager

    monkeypatch.setattr(hosted_manager, "docker", FakeDocker(), raising=False)
    await watchdog._check(bot, engine, interval=30, grace=0, threshold=2)

    status = watchdog.status(bot.id)
    assert "容器状态 exited" in "；".join(status["issues"])


@pytest.mark.asyncio
async def test_no_client_marks_issue(tmp_path, monkeypatch):
    bot = await _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(registry.manager, "peek", lambda bot_id: None)

    watchdog = BotWatchdog()
    await watchdog._check(bot, None, interval=30, grace=0, threshold=2)
    status = watchdog.status(bot.id)
    assert status["issues"] == ["协议端未连接"]
