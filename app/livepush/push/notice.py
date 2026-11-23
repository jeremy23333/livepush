"""按直播间手动群发通知。

与「真实开播」共用同一套模板占位符和渲染/派发链路，区别只有两点：

1. 事件 kind 用 ``manual``，由这里按直播间一次性生成「每个绑定目标一条」的
   ``PushJob``，因此不需要等直播状态发生变化；
2. 文案由用户在 /rooms 页填写，按直播间持久化在 ``app_setting`` 里（不新增表，
   避免老库还要做 ALTER 迁移）。

多发时按 ``SEND_INTERVAL_SEC`` 把任务的 ``next_attempt_at`` 错开，配合派发器
「一次只发一条 + 每个目标 1 秒间隔」的限速，避免几个群在同一秒里被连续 @全体。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select

from ..appconfig import get_setting, set_setting
from ..db import session_scope
from ..models import LiveEvent, LiveRoom, PushJob, PushTarget, Subscription, now_ts

# 同一个直播间反复发通知时的默认文案（占位符与开播推送完全一致）
DEFAULT_TEXT = "{uname} 直播通知\n分区：{area}\n标题：{title}\n{url}"

# 多目标时每条之间的排队间隔（秒）
SEND_INTERVAL_SEC = 2.0

# app_setting 里保存 {room_id: {text, at_all, include_cover}} 的键
NOTICE_KEY = "room_notice"


class NoticeError(Exception):
    """可以直接展示给用户的错误（没有绑定目标、直播间不存在等）。"""


@dataclass(slots=True)
class NoticeDraft:
    text: str = DEFAULT_TEXT
    at_all: bool = False
    include_cover: bool = True

    def to_dict(self) -> dict:
        return {"text": self.text, "at_all": bool(self.at_all), "include_cover": bool(self.include_cover)}


def _draft_from_dict(data: dict | None) -> NoticeDraft:
    data = data or {}
    text = str(data.get("text") or DEFAULT_TEXT)
    return NoticeDraft(
        text=text,
        at_all=bool(data.get("at_all", False)),
        include_cover=bool(data.get("include_cover", True)),
    )


async def _read_map(session) -> dict:
    raw = await get_setting(session, NOTICE_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _write_map(session, data: dict) -> None:
    await set_setting(session, NOTICE_KEY, json.dumps(data, ensure_ascii=False))


async def load_notice(room_id: int) -> NoticeDraft:
    """读该直播间上次保存的文案（没有就用默认模板）。"""
    async with session_scope() as session:
        data = await _read_map(session)
        return _draft_from_dict(data.get(str(room_id)))


async def save_notice(room_id: int, draft: NoticeDraft) -> NoticeDraft:
    async with session_scope() as session:
        data = await _read_map(session)
        data[str(room_id)] = draft.to_dict()
        await _write_map(session, data)
    return draft


async def list_targets(room_id: int) -> list[dict]:
    """该直播间当前会收到通知的目标（用于在弹窗里显示「将发往 N 个目标」）。"""
    async with session_scope() as session:
        room = await session.get(LiveRoom, room_id)
        if room is None:
            raise NoticeError("直播间不存在")
        rows = (
            await session.execute(
                select(PushTarget)
                .join(Subscription, Subscription.target_id == PushTarget.id)
                .where(
                    Subscription.room_id == room_id,
                    Subscription.enabled.is_(True),
                    PushTarget.enabled.is_(True),
                )
                .order_by(PushTarget.id)
            )
        ).scalars().all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "kind_label": "群聊" if row.kind == "group" else "私聊",
            "target_id": row.target_id,
            "name": row.name,
        }
        for row in rows
    ]


def _snapshot(room: LiveRoom, draft: NoticeDraft, *, now: int) -> dict:
    duration = 0
    if room.live_status == 1 and room.live_started_at:
        duration = max(0, now - room.live_started_at)
    return {
        "uname": room.uname,
        "title": room.title,
        "area": " / ".join(part for part in (room.area_parent, room.area_name) if part),
        "url": room.url,
        "room_id": room.room_id,
        "uid": room.uid,
        "cover_url": room.cover_url,
        "live_started_at": room.live_started_at,
        "online": 0,
        "duration": duration,
        # 手动通知自己的字段：派发器在 kind=manual 时会用它们覆盖绑定的默认值
        "text": draft.text,
        "at_all": bool(draft.at_all),
        "include_cover": bool(draft.include_cover),
        "manual": True,
    }


async def send_notice(room_id: int, draft: NoticeDraft) -> dict:
    """为每个绑定目标生成一条 pending 任务（带排队间隔），并持久化文案。"""
    now = now_ts()
    async with session_scope() as session:
        room = await session.get(LiveRoom, room_id)
        if room is None:
            raise NoticeError("直播间不存在")

        targets = (
            await session.execute(
                select(PushTarget)
                .join(Subscription, Subscription.target_id == PushTarget.id)
                .where(
                    Subscription.room_id == room_id,
                    Subscription.enabled.is_(True),
                    PushTarget.enabled.is_(True),
                )
                .order_by(PushTarget.id)
            )
        ).scalars().all()
        if not targets:
            raise NoticeError("这个直播间还没有绑定任何可用的推送目标")

        data = await _read_map(session)
        data[str(room_id)] = draft.to_dict()
        await _write_map(session, data)

        snapshot = _snapshot(room, draft, now=now)
        event = LiveEvent(
            room_id=room.id,
            kind="manual",
            prev_status=room.live_status,
            new_status=room.live_status,
            duration=snapshot["duration"],
            snapshot=json.dumps(snapshot, ensure_ascii=False),
            created_at=now,
        )
        session.add(event)
        await session.flush()

        for index, target in enumerate(targets):
            session.add(
                PushJob(
                    event_id=event.id,
                    target_id=target.id,
                    status="pending",
                    next_attempt_at=now + int(index * SEND_INTERVAL_SEC),
                    created_at=now,
                    updated_at=now,
                )
            )
        event_id = event.id
        count = len(targets)

    return {
        "event_id": event_id,
        "targets": count,
        "interval_sec": SEND_INTERVAL_SEC,
        "queued": [{"id": t.id, "kind": t.kind, "target_id": t.target_id, "name": t.name} for t in targets],
    }
