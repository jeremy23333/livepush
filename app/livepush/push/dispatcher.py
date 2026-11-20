"""推送派发器：消费 outbox（push_job），渲染 → 发送 → 记录结果。

可靠性设计：
- `UNIQUE(event_id, target_id)` 保证同一事件对同一目标只发一次；
- 失败按 2s/5s/15s 退避重试，3 次后标记 failed；
- 目标不可用（群不存在/被踢）→ 自动禁用该推送目标；
- 机器人离线 → 任务保持 pending，恢复后补发；
- 过期的下播事件直接 skipped，避免修好之后被一夜的旧通知刷屏。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from time import monotonic

from sqlalchemy import select

from ..appconfig import AppConfig, load_app_config
from ..bili.covers import CoverCache, to_base64_uri
from ..db import session_scope
from ..models import BotConnection, LiveEvent, LiveRoom, PushJob, PushTarget, Subscription, now_ts
from ..qq.base import SendResult
from ..qq.registry import manager
from ..util.timefmt import fmt_dt
from .render import RenderInput, build_message

log = logging.getLogger("livepush.push")

RETRY_DELAYS = (2, 5, 15)
RISK_BACKOFF = 300
OFFLINE_BACKOFF = 60


class Dispatcher:
    def __init__(self, covers_dir: Path, batch_size: int = 20) -> None:
        self._covers = CoverCache(covers_dir)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._config = AppConfig()
        self._batch_size = batch_size
        self._last_target_send: dict[int, float] = {}
        self._last_global = 0.0
        self.sent_count = 0
        self.failed_count = 0
        self.last_error = ""

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="livepush-dispatcher")
        log.info("推送派发器已启动")

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
        await self._covers.aclose()
        log.info("推送派发器已停止")

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ 主循环
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.process_batch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单轮失败不影响后续
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("派发异常")
                processed = 0
            timeout = 0.5 if processed >= self._batch_size else 2.0
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ 批量处理
    async def process_batch(self) -> int:
        now = now_ts()
        async with session_scope() as session:
            self._config = await load_app_config(session)
            rows = (
                await session.execute(
                    select(PushJob)
                    .where(PushJob.status == "pending", PushJob.next_attempt_at <= now)
                    .order_by(PushJob.id)
                    .limit(self._batch_size)
                )
            ).scalars().all()
            job_ids = [row.id for row in rows]
            for row in rows:
                row.status = "sending"
                row.updated_at = now

        succeeded = 0
        for job_id in job_ids:
            try:
                if await self._deliver(job_id):
                    succeeded += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("派发任务 %s 异常", job_id)
                await self._finish_failure(job_id, f"内部错误：{exc}", attempts_delta=1)
        return succeeded

    # ------------------------------------------------------------------ 单条投递
    async def _load_context(self, job_id: int) -> dict | None:
        async with session_scope() as session:
            job = await session.get(PushJob, job_id)
            if job is None or job.status != "sending":
                return None
            event = await session.get(LiveEvent, job.event_id)
            target = await session.get(PushTarget, job.target_id)
            if event is None or target is None:
                job.status = "skipped"
                job.last_error = "事件或目标已删除"
                return None
            room = await session.get(LiveRoom, event.room_id)
            bot_conn = await session.get(BotConnection, target.bot_connection_id)
            sub = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.room_id == event.room_id, Subscription.target_id == target.id
                    )
                )
            ).scalar_one_or_none()
            if bot_conn is None:
                job.status = "skipped"
                job.last_error = "机器人连接已删除"
                return None
            snapshot = {}
            try:
                snapshot = json.loads(event.snapshot or "{}")
            except ValueError:
                snapshot = {}
            return {
                "job_id": job.id,
                "attempts": job.attempts,
                "kind": event.kind,
                "duration": event.duration,
                "created_at": event.created_at,
                "snapshot": snapshot,
                "room_uname": room.uname if room else "",
                "room_title": room.title if room else "",
                "target_id": target.id,
                "target_kind": target.kind,
                "target_external_id": target.target_id,
                "target_name": target.name,
                "at_all": bool(sub.at_all) if sub else False,
                "include_cover": bool(sub.include_cover) if sub else True,
                "template_override": sub.message_template if sub else "",
                "bot_id": bot_conn.id,
                "bot_name": bot_conn.name,
                "bot_transport": bot_conn.transport,
                "bot_mode": bot_conn.mode,
                "bot_enabled": bot_conn.enabled,
                "bot_conn": bot_conn,
            }

    async def _deliver(self, job_id: int) -> bool:
        ctx = await self._load_context(job_id)
        if ctx is None:
            return False

        # 过期事件直接跳过（避免长时间停机后集中补发旧通知）
        if ctx["kind"] == "live_off":
            age = now_ts() - (ctx["created_at"] or 0)
            if age > self._config.max_event_age_hours * 3600:
                await self._mark_skipped(job_id, f"事件已过期（{age // 3600} 小时前）")
                return False

        if not ctx["bot_enabled"]:
            await self._finish_failure(job_id, "机器人连接已停用", attempts_delta=1)
            return False

        client = await manager.get(ctx["bot_conn"])
        if not client.connected:
            # 机器人离线：保留任务，稍后再试（不计入失败次数）
            await self._finish_offline(job_id)
            return False

        snapshot = ctx["snapshot"]
        data = RenderInput(
            kind=ctx["kind"],
            uname=snapshot.get("uname") or ctx["room_uname"],
            title=snapshot.get("title") or ctx["room_title"],
            area=snapshot.get("area") or "",
            url=snapshot.get("url") or "",
            duration=ctx["duration"] or int(snapshot.get("duration") or 0),
            live_started_at=int(snapshot.get("live_started_at") or 0),
            online=int(snapshot.get("online") or 0),
            room_id=int(snapshot.get("room_id") or 0),
            uid=int(snapshot.get("uid") or 0),
            include_cover=ctx["include_cover"],
            at_all=ctx["at_all"],
            template_override=ctx["template_override"],
        )

        cover_uri = None
        if self._config.cover_mode != "off" and ctx["include_cover"] and data.kind in ("live_on", "title_change"):
            cover_url = snapshot.get("cover_url") or ""
            if cover_url:
                if self._config.cover_mode == "url":
                    cover_uri = cover_url
                else:
                    path = await self._covers.get(cover_url)
                    if path is not None:
                        try:
                            cover_uri = to_base64_uri(path.read_bytes())
                        except OSError:
                            cover_uri = None

        # 第一次失败后不再 @全体，优先保证送达
        if ctx["attempts"] >= 1:
            data.at_all = False
        # @全体只对群聊有意义
        if ctx["target_kind"] != "group":
            data.at_all = False
        if data.at_all and ctx["target_kind"] == "group":
            if client.at_all_supported:
                remain = await client.get_at_all_remain(ctx["target_external_id"])
                if not remain or not remain.get("can_at_all"):
                    data.at_all = False
            # Milky 没有配额接口，直接发，失败再降级

        message = build_message(data, self._config, cover_uri)
        if not message.plain_text().strip() and not message.has_image:
            await self._mark_skipped(job_id, "渲染结果为空")
            return False

        await self._respect_rate_limit(ctx["target_id"])
        result = await client.send_message(ctx["target_kind"], ctx["target_external_id"], message)

        if result.ok:
            await self._finish_success(job_id, message.preview())
            return True

        await self._handle_failure(job_id, ctx, result)
        return False

    # ------------------------------------------------------------------ 结果处理
    async def _respect_rate_limit(self, target_id: int) -> None:
        global_gap = 0.2
        target_gap = 1.0
        now = monotonic()
        wait = max(0.0, self._last_global + global_gap - now)
        wait = max(wait, self._last_target_send.get(target_id, 0.0) + target_gap - now)
        if wait > 0:
            await asyncio.sleep(min(wait, 5.0))
        self._last_global = monotonic()
        self._last_target_send[target_id] = monotonic()

    async def _handle_failure(self, job_id: int, ctx: dict, result: SendResult) -> None:
        if result.fatal:
            async with session_scope() as session:
                target = await session.get(PushTarget, ctx["target_id"])
                if target is not None:
                    target.enabled = False
                    target.updated_at = now_ts()
                job = await session.get(PushJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.last_error = f"目标不可用，已自动禁用：{result.error}"
                    job.updated_at = now_ts()
            log.warning("推送目标 %s 不可用，已禁用：%s", ctx["target_name"], result.error)
            return

        if result.risk:
            await self._reschedule(job_id, result.error, delay=RISK_BACKOFF, attempts_delta=1)
            return
        if result.unknown:
            await self._finish_offline(job_id, result.error)
            return
        await self._finish_failure(job_id, result.error, attempts_delta=1)

    async def _finish_success(self, job_id: int, preview: str) -> None:
        self.sent_count += 1
        async with session_scope() as session:
            job = await session.get(PushJob, job_id)
            if job is None:
                return
            job.status = "sent"
            job.attempts = (job.attempts or 0) + 1
            job.last_error = ""
            job.preview = preview
            job.updated_at = now_ts()

    async def _reschedule(self, job_id: int, error: str, *, delay: int, attempts_delta: int = 0) -> None:
        async with session_scope() as session:
            job = await session.get(PushJob, job_id)
            if job is None:
                return
            job.attempts = (job.attempts or 0) + attempts_delta
            job.status = "pending"
            job.last_error = error[:500]
            job.next_attempt_at = now_ts() + delay
            job.updated_at = now_ts()

    async def _finish_offline(self, job_id: int, error: str = "机器人未连接") -> None:
        self.last_error = error
        async with session_scope() as session:
            job = await session.get(PushJob, job_id)
            if job is None:
                return
            job.status = "pending"
            job.last_error = error[:500]
            job.next_attempt_at = now_ts() + OFFLINE_BACKOFF
            job.updated_at = now_ts()

    async def _finish_failure(self, job_id: int, error: str, attempts_delta: int = 1) -> None:
        self.failed_count += 1
        async with session_scope() as session:
            job = await session.get(PushJob, job_id)
            if job is None:
                return
            attempts = (job.attempts or 0) + attempts_delta
            job.attempts = attempts
            job.last_error = error[:500]
            job.updated_at = now_ts()
            if attempts >= len(RETRY_DELAYS):
                job.status = "failed"
            else:
                job.status = "pending"
                job.next_attempt_at = now_ts() + RETRY_DELAYS[max(0, attempts - 1)]
        log.warning("推送失败（job=%s）：%s", job_id, error)

    async def _mark_skipped(self, job_id: int, reason: str) -> None:
        async with session_scope() as session:
            job = await session.get(PushJob, job_id)
            if job is None:
                return
            job.status = "skipped"
            job.last_error = reason
            job.updated_at = now_ts()


def format_time(ts: int) -> str:
    return fmt_dt(ts)
