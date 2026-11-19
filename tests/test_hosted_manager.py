"""托管安装编排：用 FakeDocker + 假探测跑通「安装 → 绑定」，不碰真 Docker/网络。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from livepush.config import Runtime
from livepush.db import init_engine, init_schema, session_scope
from livepush.models import BotConnection, HostedEngine
from livepush.qq.hosted import manager as mgr
from livepush.qq.hosted.manager import HostedError


class FakeDocker:
    def __init__(self, version: str = "24.0.7") -> None:
        self.version = version
        self.container = ""
        self.state_name = ""
        self.calls: list[tuple[str, str]] = []
        self.run_args: list[str] = []

    async def available(self) -> str:
        return self.version

    async def load(self, path) -> str:
        self.calls.append(("load", str(path)))
        return "livepush/napcat:4.18.42"

    async def run(self, args, image) -> str:
        self.calls.append(("run", image))
        self.run_args = list(args)
        self.container = args[args.index("--name") + 1]
        self.state_name = "running"
        return "container-id"

    async def state(self, name) -> str:
        return self.state_name if name == self.container else ""

    async def start(self, name) -> None:
        self.state_name = "running"

    async def stop(self, name) -> None:
        self.state_name = "exited"

    async def rm(self, name) -> None:
        self.state_name = ""

    async def logs(self, name, tail: int = 80) -> str:
        return "container log line"


async def setup(tmp_path, monkeypatch, *, backend: str = "napcat", docker_up: bool = True):
    monkeypatch.setenv("LIVEPUSH_DATA_DIR", str(tmp_path))
    Runtime.load()
    await init_engine(Runtime.settings.db_url)
    await init_schema()

    async with session_scope() as session:
        bot = BotConnection(
            name="测试号", transport="onebot11", mode="hosted", backend=backend, uin=12345
        )
        session.add(bot)
        await session.flush()
        bot_id = bot.id

    fake = FakeDocker()
    if not docker_up:
        fake.version = ""

    hosted = mgr.hosted_manager
    hosted.docker = fake
    hosted.jobs.clear()
    hosted.PROBE_TIMEOUT = 3

    async def fake_fetch(artifact, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"fake tar")
        return Path(dest)

    async def fake_wait(urls, token="", *, timeout=120.0, interval=2.0):
        return urls[0], {"self_id": 12345, "nickname": "测试号"}

    async def fake_probe(url, token="", timeout=6.0):
        return {"self_id": 12345, "nickname": "测试号"}

    monkeypatch.setattr(mgr, "fetch", fake_fetch)
    monkeypatch.setattr(mgr, "wait_for_onebot11", fake_wait)
    hosted.probe = fake_probe

    # 避免真的去连 OneBot11：绑定阶段的 registry 调用是副作用，不是本用例要测的
    async def noop_stop(_bot_id):
        return None

    async def noop_get(_bot):
        return None

    monkeypatch.setattr(mgr.registry.manager, "stop", noop_stop)
    monkeypatch.setattr(mgr.registry.manager, "get", noop_get)
    return bot_id, fake


async def wait_job(job, timeout: float = 10.0):
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not job.done and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return job


@pytest.mark.asyncio
async def test_install_binds_bot_and_persists_engine(tmp_path, monkeypatch):
    bot_id, fake = await setup(tmp_path, monkeypatch)
    job = mgr.hosted_manager.install(bot_id)
    await wait_job(job)

    assert job.ok, job.error
    assert [name for name, _ in fake.calls] == ["load", "run"]

    async with session_scope() as session:
        bot = await session.get(BotConnection, bot_id)
        engine = (
            await session.execute(
                select(HostedEngine).where(HostedEngine.bot_connection_id == bot_id)
            )
        ).scalar_one()
    assert bot.transport == "onebot11"
    assert bot.ws_url == f"ws://127.0.0.1:{engine.host_ws_port}"
    assert bot.self_id == 12345
    assert engine.state == "running"
    assert engine.image_ref == "livepush/napcat:4.18.42"


@pytest.mark.asyncio
async def test_install_fails_without_docker(tmp_path, monkeypatch):
    bot_id, _ = await setup(tmp_path, monkeypatch, docker_up=False)
    job = mgr.hosted_manager.install(bot_id)
    await wait_job(job)

    assert job.done and not job.ok
    assert "Docker" in job.error

    async with session_scope() as session:
        bot = await session.get(BotConnection, bot_id)
    assert bot.ws_url == ""


@pytest.mark.asyncio
async def test_install_requires_hosted_mode(tmp_path, monkeypatch):
    bot_id, _ = await setup(tmp_path, monkeypatch)
    async with session_scope() as session:
        bot = await session.get(BotConnection, bot_id)
        bot.mode = "builtin"

    job = mgr.hosted_manager.install(bot_id)
    await wait_job(job)
    assert job.done and not job.ok
    assert "托管" in job.error


@pytest.mark.asyncio
async def test_status_reports_installed_and_container_state(tmp_path, monkeypatch):
    bot_id, fake = await setup(tmp_path, monkeypatch)
    job = mgr.hosted_manager.install(bot_id)
    await wait_job(job)

    status = await mgr.hosted_manager.status(bot_id, host="10.0.0.5")
    assert status["installed"] is True
    assert status["state"] == "running"
    assert status["ws_url"].startswith("ws://10.0.0.5:")
    assert status["webui_url"].startswith("http://10.0.0.5:")
    assert "log line" in status["log_tail"]


@pytest.mark.asyncio
async def test_attach_raises_when_probe_fails(tmp_path, monkeypatch):
    bot_id, _ = await setup(tmp_path, monkeypatch)
    job = mgr.hosted_manager.install(bot_id)
    await wait_job(job)

    async def dead_probe(url, token="", timeout=6.0):
        return None

    mgr.hosted_manager.probe = dead_probe
    with pytest.raises(HostedError) as exc:
        await mgr.hosted_manager.attach(bot_id)
    assert "连不上" in str(exc.value)


@pytest.mark.asyncio
async def test_remove_drops_engine_and_container(tmp_path, monkeypatch):
    bot_id, fake = await setup(tmp_path, monkeypatch)
    job = mgr.hosted_manager.install(bot_id)
    await wait_job(job)

    await mgr.hosted_manager.remove(bot_id)
    assert fake.state_name == ""
    status = await mgr.hosted_manager.status(bot_id)
    assert status["installed"] is False
