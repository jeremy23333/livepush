"""按直播间手动通知：持久化、入队间隔、渲染与派发。"""

from __future__ import annotations

import json

import pytest

from sqlalchemy import select

from livepush.appconfig import AppConfig
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
from livepush.push import notice
from livepush.push.dispatcher import Dispatcher
from livepush.push.notice import NoticeDraft, NoticeError, send_notice
from livepush.push.render import RenderInput, build_message
from livepush.qq.base import SendResult


class FakeClient:
    transport = "onebot11"

    def __init__(self) -> None:
        self.connected = True
        self.at_all_supported = False
        self.sent: list[tuple] = []

    async def send_message(self, kind: str, target_id: str, message) -> SendResult:  # noqa: ANN001
        self.sent.append((kind, target_id, message))
        return SendResult.success()

    async def get_at_all_remain(self, group_id: str):  # noqa: ANN001
        return None


class FakeManager:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    async def get(self, conn, *, autostart: bool = True):  # noqa: ANN001
        return self.client


@pytest.fixture
async def db(data_dir):
    Runtime.load()
    await init_engine(Runtime.settings.db_url)
    await init_schema()
    return data_dir


async def seed(*, targets: int = 2, subscribed: bool = True) -> dict:
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
        room = LiveRoom(
            uid=672328094,
            room_id=22637261,
            uname="嘉然今天吃什么",
            title="【突击】鸣潮3.8！",
            area_parent="手游",
            area_name="鸣潮",
            live_status=1,
            live_started_at=now_ts() - 1800,
            room_state="ok",
        )
        session.add(room)
        await session.flush()
        ids = []
        for index in range(targets):
            target = PushTarget(
                bot_connection_id=bot.id,
                kind="group" if index == 0 else "private",
                target_id=f"10000{index}",
                name=f"目标{index}",
            )
            session.add(target)
            await session.flush()
            if subscribed:
                session.add(Subscription(room_id=room.id, target_id=target.id))
            ids.append(target.id)
        return {"room": room.id, "targets": ids}


async def test_send_notice_queues_targets_with_interval(db):
    ids = await seed()
    draft = NoticeDraft(text="{uname} 马上开播：{title}\n{url}", at_all=True, include_cover=False)
    result = await send_notice(ids["room"], draft)

    assert result["targets"] == 2
    assert result["interval_sec"] == notice.SEND_INTERVAL_SEC

    async with session_scope() as session:
        event = await session.get(LiveEvent, result["event_id"])
        assert event.kind == "manual"
        snapshot = json.loads(event.snapshot)
        assert snapshot["text"] == draft.text
        assert snapshot["at_all"] is True
        assert snapshot["include_cover"] is False
        assert snapshot["url"] == "https://live.bilibili.com/22637261"
        assert snapshot["duration"] >= 1800

        jobs = (
            await session.execute(select(PushJob).where(PushJob.event_id == event.id).order_by(PushJob.id))
        ).scalars().all()
        assert len(jobs) == 2
        assert all(row.status == "pending" for row in jobs)
        # 第二条要等一个间隔，避免同一秒连发
        assert jobs[1].next_attempt_at - jobs[0].next_attempt_at == int(notice.SEND_INTERVAL_SEC)


async def test_send_notice_without_targets_raises(db):
    ids = await seed(targets=0, subscribed=False)
    with pytest.raises(NoticeError):
        await send_notice(ids["room"], NoticeDraft())


async def test_disabled_target_is_skipped(db):
    ids = await seed(targets=2)
    async with session_scope() as session:
        target = await session.get(PushTarget, ids["targets"][1])
        target.enabled = False
    result = await send_notice(ids["room"], NoticeDraft())
    assert result["targets"] == 1


async def test_notice_draft_roundtrip(db):
    ids = await seed()
    draft = NoticeDraft(text="自定义文案 {uname}", at_all=True, include_cover=False)
    await notice.save_notice(ids["room"], draft)
    loaded = await notice.load_notice(ids["room"])
    assert loaded.text == draft.text
    assert loaded.at_all is True
    assert loaded.include_cover is False

    # 没保存过的直播间拿到默认文案
    other = await notice.load_notice(999999)
    assert other.text == notice.DEFAULT_TEXT


async def test_manual_message_renders_placeholders_and_extras():
    cfg = AppConfig()
    data = RenderInput(
        kind="manual",
        uname="嘉然今天吃什么",
        title="【突击】鸣潮3.8！",
        area="手游 / 鸣潮",
        url="https://live.bilibili.com/22637261",
        duration=1800,
        at_all=True,
        include_cover=True,
        template_override="{uname} 直播通知\n{title}｜{duration}\n{url}",
    )
    message = build_message(data, cfg, "data:image/png;base64,AAAA")
    text = message.plain_text()
    assert "嘉然今天吃什么 直播通知" in text
    assert "【突击】鸣潮3.8！｜30 分钟" in text
    assert "https://live.bilibili.com/22637261" in text
    assert message.has_mention_all is True
    assert message.has_image is True


async def test_dispatcher_sends_manual_notice(db, monkeypatch):
    ids = await seed(targets=1)
    await send_notice(ids["room"], NoticeDraft(text="手动通知：{uname} 在直播 {title}", at_all=True))

    async with session_scope() as session:
        row = (await session.execute(select(PushJob).order_by(PushJob.id))).scalars().first()
        assert row is not None
        row.next_attempt_at = 0
        await session.flush()
        job_id = row.id

    fake = FakeClient()
    monkeypatch.setattr("livepush.push.dispatcher.manager", FakeManager(fake))
    dispatcher = Dispatcher(db / "covers")
    assert await dispatcher.process_batch() == 1

    kind, target_id, message = fake.sent[0]
    assert kind == "group"
    assert target_id == "100000"
    assert message.plain_text() == "手动通知：嘉然今天吃什么 在直播 【突击】鸣潮3.8！"
    assert message.has_mention_all is True

    async with session_scope() as session:
        row = await session.get(PushJob, job_id)
        assert row.status == "sent"
        assert row.preview
    await dispatcher.stop()
