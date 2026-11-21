"""轮询引擎：按间隔批量拉取状态 → 跑状态机 → 落事件与推送任务。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from datetime import datetime

from sqlalchemy import select

from ..appconfig import AppConfig, load_app_config
from ..bili.client import BiliClient, BiliError, BiliRiskControl
from ..bili.parse import RoomSnapshot
from ..db import session_scope
from ..models import BotConnection, LiveEvent, LiveRoom, PushJob, PushTarget, Subscription, now_ts
from ..util.timefmt import TZ
from .events import bus
from .state import RoomState, decide

log = logging.getLogger("livepush.monitor")

EVENT_LABELS = {
    "live_on": "开播",
    "live_off": "下播",
    "title_change": "标题变更",
    "area_change": "分区变更",
}


class MonitorEngine:
    def __init__(self, covers_dir) -> None:
        self._covers_dir = covers_dir
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.bili: BiliClient | None = None
        self._bili_signature: tuple[str, str] | None = None
        self.config = AppConfig()
        self.last_tick_at = 0
        self.tick_count = 0
        self.last_error = ""
        self.any_live = False
        self.room_count = 0
        self.wakeup = asyncio.Event()  # 有新推送任务时唤醒派发器
        self._pending_since: dict[int, int] = {}  # 房间 → 本轮待确认状态首次观测时刻

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        await self._ensure_bili(self.config)
        with contextlib.suppress(Exception):
            await self.backfill_room_ids()
        self._task = asyncio.create_task(self._loop(), name="livepush-monitor")
        log.info("监控引擎已启动")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.bili is not None:
            await self.bili.aclose()
            self.bili = None
            self._bili_signature = None
        log.info("监控引擎已停止")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger_now(self) -> None:
        """让下一次 tick 立刻发生（UI 的「立即检查」）。

        注意要唤醒等待中的循环：只置标志位是没用的，因为循环正阻塞在 sleep 里。
        """
        self._wake.set()

    # ------------------------------------------------------------------ 主循环
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单轮失败不拖垮循环
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("轮询异常")

            interval = self.config.interval_for(self.any_live, datetime.now(TZ).hour)
            jitter = interval * self.config.jitter_ratio
            delay = max(10.0, interval + random.uniform(-jitter, jitter))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return

    async def backfill_room_ids(self) -> int:
        """给历史数据里 `room_id == 0` 的直播间补房间号。

        这类行会让推送链接退化成空间页（以前更糟：伪造 `live/<uid>`）。先批量接口补，
        批量没给的再逐个用 `getRoomInfoOld` 补；失败只记日志，绝不阻塞启动。
        """
        if self.bili is None:
            return 0
        async with session_scope() as session:
            rows = (
                await session.execute(select(LiveRoom).where(LiveRoom.room_id == 0))
            ).scalars().all()
            targets = [(row.id, row.uid) for row in rows if row.uid]
        if not targets:
            return 0

        uids = [uid for _, uid in targets]
        try:
            snaps = await self.bili.get_status_by_uids(uids, chunk_size=50, chunk_delay=0.3)
        except BiliError as exc:
            log.warning("补全房间号：批量查询失败（改用逐个查询）：%s", exc)
            snaps = {}

        found: dict[int, tuple[int, int]] = {}
        for room_pk, uid in targets:
            snap = snaps.get(uid)
            room_id = snap.room_id if snap else 0
            short_id = snap.short_id if snap else 0
            if not room_id:
                with contextlib.suppress(BiliError):
                    room_id, short_id = await self.bili.room_id_by_uid(uid)
                    await asyncio.sleep(0.3)  # 逐个补全时放慢，避免触发风控
            if room_id:
                found[room_pk] = (room_id, short_id)

        if not found:
            return 0
        async with session_scope() as session:
            for room_pk, (room_id, short_id) in found.items():
                row = await session.get(LiveRoom, room_pk)
                if row is None:
                    continue
                row.room_id = row.room_id or room_id
                row.short_id = row.short_id or short_id
                row.updated_at = now_ts()
        log.info("已补全 %s 个直播间的房间号", len(found))
        return len(found)

    async def _ensure_bili(self, config: AppConfig) -> None:
        signature = (config.bili_cookie, config.bili_proxy)
        if self.bili is not None and signature == self._bili_signature:
            return
        if self.bili is not None:
            await self.bili.aclose()
        self.bili = BiliClient(
            covers_dir=self._covers_dir,
            cookie=config.bili_cookie,
            proxy=config.bili_proxy,
            chunk_delay=config.chunk_delay_ms / 1000,
        )
        self._bili_signature = signature

    # ------------------------------------------------------------------ 一轮采集
    async def tick(self) -> None:
        async with session_scope() as session:
            config = await load_app_config(session)
            self.config = config
            rows = (
                await session.execute(
                    select(LiveRoom).where(LiveRoom.enabled.is_(True)).order_by(LiveRoom.id)
                )
            ).scalars().all()
            rooms = [
                {
                    "id": r.id,
                    "uid": r.uid,
                    "room_state": r.room_state,
                    "live_status": r.live_status,
                    "live_started_at": r.live_started_at,
                    "pending_status": r.pending_status,
                    "pending_count": r.pending_count,
                    "title": r.title,
                    "area_name": r.area_name,
                    "notify_on_add": r.notify_on_add,
                }
                for r in rows
            ]

        self.room_count = len(rooms)
        if not rooms:
            self.any_live = False
            self.last_tick_at = now_ts()
            self.tick_count += 1
            return

        await self._ensure_bili(config)
        assert self.bili is not None

        try:
            snapshots = await self.bili.get_status_by_uids(
                [r["uid"] for r in rooms], config.chunk_size, config.chunk_delay_ms / 1000
            )
        except (BiliRiskControl, BiliError) as exc:
            self.last_error = str(exc)
            log.warning("本轮采集失败：%s", exc)
            await self._mark_failure([r["id"] for r in rooms])
            self.last_tick_at = now_ts()
            self.tick_count += 1
            return

        self.last_error = ""
        self.any_live = any(snap.live_norm for snap in snapshots.values())
        self.last_tick_at = now_ts()
        self.tick_count += 1

        async with session_scope() as session:
            for info in rooms:
                row = await session.get(LiveRoom, info["id"])
                if row is None:
                    continue
                snap = snapshots.get(row.uid)
                if snap is None:
                    # 查询成功但该 uid 不在返回里 → 没有直播间
                    row.room_state = "no_room"
                    row.live_status = 0
                    row.live_started_at = 0
                    row.pending_status = None
                    row.pending_count = 0
                    row.fail_streak = 0
                    self._pending_since.pop(info["id"], None)
                    row.last_checked_at = now_ts()
                    row.updated_at = now_ts()
                    continue
                await self._apply_snapshot(session, row, snap, config)

        if self.wakeup.is_set():
            log.debug("有新的推送任务等待派发")

    async def _apply_snapshot(self, session, row: LiveRoom, snap: RoomSnapshot, config: AppConfig) -> None:
        now = now_ts()
        previous_norm = row.live_norm
        previous_title = row.title
        started_before = row.live_started_at
        state = RoomState(
            room_state=row.room_state,
            live_status=row.live_status,
            live_started_at=row.live_started_at,
            pending_status=row.pending_status,
            pending_count=row.pending_count,
            title=row.title,
            area_name=row.area_name,
            notify_on_add=row.notify_on_add,
        )
        state.pending_since = self._pending_since.get(row.id, 0)
        decision = decide(
            state,
            snap,
            now=now,
            on_confirm_polls=config.on_confirm_polls,
            off_confirm_polls=config.off_confirm_polls,
        )
        if decision.pending_status is None:
            self._pending_since.pop(row.id, None)
        else:
            self._pending_since[row.id] = decision.pending_since or now

        # 更新直播间快照
        row.uid = snap.uid or row.uid
        row.room_id = snap.room_id or row.room_id
        row.short_id = snap.short_id or row.short_id
        row.uname = snap.uname or row.uname
        row.face = snap.face or row.face
        row.title = snap.title or row.title
        row.area_name = snap.area_name or row.area_name
        row.area_parent = snap.area_parent or row.area_parent
        row.cover_url = snap.cover_url or row.cover_url
        row.live_status = decision.live_status
        row.live_started_at = decision.live_started_at
        row.pending_status = decision.pending_status
        row.pending_count = decision.pending_count
        row.room_state = decision.room_state
        row.fail_streak = 0
        row.last_checked_at = now
        row.last_ok_at = now
        row.updated_at = now

        if not decision.events:
            return

        for kind in decision.events:
            if await self._recently_emitted(session, row.id, kind, config, now):
                log.debug("事件去抖跳过：room=%s kind=%s", row.id, kind)
                continue
            event = LiveEvent(
                room_id=row.id,
                kind=kind,
                prev_status=previous_norm,
                new_status=snap.live_norm,
                duration=decision.duration if kind == "live_off" else 0,
                snapshot=json.dumps(
                    {
                        "uid": snap.uid,
                        "room_id": snap.display_room_id,
                        "uname": row.uname,
                        "title": row.title,
                        "area": snap.area,
                        "url": snap.url,
                        "cover_url": row.cover_url,
                        "duration": decision.duration,
                        "live_started_at": started_before,
                        "live_status": snap.live_status,
                        "online": snap.online,
                    },
                    ensure_ascii=False,
                ),
                created_at=now,
            )
            session.add(event)
            await session.flush()
            await self._create_jobs(session, event, row, now)
            bus.publish(
                {
                    "type": "live_event",
                    "id": event.id,
                    "room_id": row.id,
                    "kind": kind,
                    "label": EVENT_LABELS.get(kind, kind),
                    "uname": row.uname,
                    "title": row.title if kind != "title_change" else row.title,
                    "prev_title": previous_title,
                    "url": snap.url,
                    "duration": decision.duration,
                    "at": now,
                }
            )
            log.info("事件：%s %s（room=%s）", row.uname, EVENT_LABELS.get(kind, kind), row.id)

    async def _recently_emitted(self, session, room_id: int, kind: str, config: AppConfig, now: int) -> bool:
        if config.min_event_interval_sec <= 0:
            return False
        last = (
            await session.execute(
                select(LiveEvent.created_at)
                .where(LiveEvent.room_id == room_id, LiveEvent.kind == kind)
                .order_by(LiveEvent.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return last is not None and now - last < config.min_event_interval_sec

    async def _create_jobs(self, session, event: LiveEvent, room: LiveRoom, now: int) -> None:
        rows = (
            await session.execute(
                select(Subscription, PushTarget, BotConnection)
                .join(PushTarget, Subscription.target_id == PushTarget.id)
                .join(BotConnection, PushTarget.bot_connection_id == BotConnection.id)
                .where(
                    Subscription.room_id == room.id,
                    Subscription.enabled.is_(True),
                    PushTarget.enabled.is_(True),
                    BotConnection.enabled.is_(True),
                )
            )
        ).all()
        for sub, target, _bot in rows:
            if event.kind == "live_on" and not sub.notify_live_on:
                continue
            if event.kind == "live_off" and not sub.notify_live_off:
                continue
            if event.kind in ("title_change", "area_change") and not sub.notify_title_change:
                continue
            session.add(
                PushJob(
                    event_id=event.id,
                    target_id=target.id,
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
        self.wakeup.set()

    async def _mark_failure(self, room_ids: list[int]) -> None:
        async with session_scope() as session:
            for room_id in room_ids:
                row = await session.get(LiveRoom, room_id)
                if row is None:
                    continue
                row.fail_streak = (row.fail_streak or 0) + 1
                row.last_checked_at = now_ts()
                if row.fail_streak >= 3:
                    row.room_state = "stale"
        # 采集失败绝不改 live_status —— 避免假下播

    # ------------------------------------------------------------------ 状态上报
    def status(self) -> dict:
        return {
            "running": self.running,
            "last_tick_at": self.last_tick_at,
            "tick_count": self.tick_count,
            "last_error": self.last_error,
            "any_live": self.any_live,
            "room_count": self.room_count,
            "risk": {
                "blocked": bool(self.bili and self.bili.blocked),
                "remaining": self.bili.blocked_remaining if self.bili else 0,
                "reason": self.bili.risk_reason if self.bili else "",
            },
            "config": self.config.to_dict(),
        }
