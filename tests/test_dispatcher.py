"""推送派发器（outbox）测试。"""

from __future__ import annotations

import json

import pytest

from livepush.bili.covers import CoverCache  # noqa: F401  (确保模块可导入)
from livepush.config import Runtime
from livepush.db import init_engine, init_schema, session_scope
from livepush.models import (
    BotConnection,
    LiveEvent,
    LiveRoom,
    PushJob,
    PushTarget,
    Subscription,
    now_ts,
)
from livepush.push.dispatcher import Dispatcher
from livepush.qq.base import SendResult


class FakeClient:
    transport = "onebot11"

    def __init__(self, result: SendResult | None = None, connected: bool = True) -> None:
        self.connected = connected
        self.at_all_supported = False
        self.sent: list[tuple] = []
        self.result = result or SendResult.success()

    async def send_message(self, kind: str, target_id: str, message):  # noqa: ANN001
        self.sent.append((kind, target_id, message))
        return self.result

    async def get_at_all_remain(self, group_id: str):  # noqa: ANN001
        return None


class FakeManager:
    def __init__(self, client) -> None:
        self.client = client

    async def get(self, conn, *, autostart: bool = True):  # noqa: ANN001
        return self.client


@pytest.fixture
async def db(data_dir):
    Runtime.load()
    await init_engine(Runtime.settings.db_url)
    await init_schema()
    return data_dir


async def seed(kind: str = "live_on", created_offset: int = 0) -> dict:
    async with session_scope() as session:
        bot = BotConnection(
            name="测试号",
            transport="onebot11",
            mode="external",
            backend="onebot11",
            ws_url="ws://127.0.0.1:1",
            state="connected",
        )
        session.add(bot)
        await session.flush()
        room = LiveRoom(uid=1, room_id=10, uname="主播", title="标题", live_status=1, room_state="ok")
        session.add(room)
        await session.flush()
        target = PushTarget(bot_connection_id=bot.id, kind="group", target_id="123456", name="测试群")
        session.add(target)
        await session.flush()
        session.add(Subscription(room_id=room.id, target_id=target.id))
        event = LiveEvent(
            room_id=room.id,
            kind=kind,
            prev_status=0,
            new_status=1,
            duration=60,
            snapshot=json.dumps(
                {
                    "uid": 1,
                    "room_id": 10,
                    "uname": "主播",
                    "title": "标题",
                    "area": "生活 / 日常",
                    "url": "https://live.bilibili.com/10",
                    "cover_url": "",
                },
                ensure_ascii=False,
            ),
            created_at=now_ts() - created_offset,
        )
        session.add(event)
        await session.flush()
        job = PushJob(event_id=event.id, target_id=target.id, status="pending", next_attempt_at=0)
        session.add(job)
        await session.flush()
        return {"job": job.id, "target": target.id, "room": room.id, "event": event.id}


async def test_send_success_and_idempotent(db, monkeypatch):
    ids = await seed()
    fake = FakeClient()
    monkeypatch.setattr("livepush.push.dispatcher.manager", FakeManager(fake))
    dispatcher = Dispatcher(Path_covers(db))

    assert await dispatcher.process_batch() == 1
    assert len(fake.sent) == 1
    kind, target_id, message = fake.sent[0]
    assert kind == "group"
    assert target_id == "123456"
    assert "开播啦" in message.plain_text()

    async with session_scope() as session:
        job = await session.get(PushJob, ids["job"])
        assert job.status == "sent"
        assert job.preview

    # 再跑一次不应重复发送
    assert await dispatcher.process_batch() == 0
    assert len(fake.sent) == 1
    await dispatcher.stop()


async def test_fatal_error_disables_target(db, monkeypatch):
    ids = await seed()
    fake = FakeClient(SendResult(ok=False, fatal=True, error="GROUP_NOT_FOUND"))
    monkeypatch.setattr("livepush.push.dispatcher.manager", FakeManager(fake))
    dispatcher = Dispatcher(Path_covers(db))
    await dispatcher.process_batch()

    async with session_scope() as session:
        target = await session.get(PushTarget, ids["target"])
        job = await session.get(PushJob, ids["job"])
        assert target.enabled is False
        assert job.status == "failed"
    await dispatcher.stop()


async def test_offline_keeps_job_pending(db, monkeypatch):
    ids = await seed()
    fake = FakeClient(connected=False)
    monkeypatch.setattr("livepush.push.dispatcher.manager", FakeManager(fake))
    dispatcher = Dispatcher(Path_covers(db))
    await dispatcher.process_batch()

    async with session_scope() as session:
        job = await session.get(PushJob, ids["job"])
        assert job.status == "pending"
        assert job.attempts == 0
        assert job.next_attempt_at > 0
    await dispatcher.stop()


async def test_stale_offline_event_is_skipped(db, monkeypatch):
    ids = await seed(kind="live_off", created_offset=10 * 3600)
    fake = FakeClient()
    monkeypatch.setattr("livepush.push.dispatcher.manager", FakeManager(fake))
    dispatcher = Dispatcher(Path_covers(db))
    await dispatcher.process_batch()

    async with session_scope() as session:
        job = await session.get(PushJob, ids["job"])
        assert job.status == "skipped"
    assert fake.sent == []
    await dispatcher.stop()


def Path_covers(data_dir):
    return data_dir / "covers"
