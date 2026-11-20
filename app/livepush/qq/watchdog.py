"""QQ 账号掉线看门狗。

风控踢下线时协议端通常**不会**断开 WebSocket：
- NapCat 的 `get_status` 仍返回 online:true（进程在、协议层已断）；
- SnowLuma 的心跳照发，但 `status.good` 在约 105 秒后翻 false。

所以这里组合多个判据，而不是只看连接态：
1. WS 连接是否存在、`get_login_info` 是否还能往返；
2. 心跳年龄（间隔的 3 倍还没心跳就算异常）、心跳里的 online / good；
3. 协议端推来的上下线 notice（bot_status / offline 等）；
4. 托管容器的运行状态与日志关键词（账号已下线 / KickedOffline 等）。

只告警，不自动重启：被踢后立即重登往往触发更严的风控。
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from ..appconfig import load_app_config
from ..db import session_scope
from ..models import BotConnection, HostedEngine
from . import registry
from .hosted import hosted_manager

log = logging.getLogger("livepush.qq.watchdog")

#: 容器日志里出现这些词，基本可以断定是被踢/登录失效
LOG_KEYWORDS = (
    "账号已下线",
    "已下线",
    "KickedOffline",
    "kicked",
    "login expired",
    "登录已失效",
    "被踢",
    "下线通知",
)

#: 同一个 bot 最多每 5 分钟打一次告警日志（告警管理器还有自己的冷却）
WARN_GAP = 300.0


class BotWatchdog:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.health: dict[int, dict] = {}
        self._good_fail: dict[int, int] = {}
        self._suspect_since: dict[int, float] = {}
        self._last_warn: dict[int, float] = {}
        self._last_alert_at: dict[int, float] = {}

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="bot-watchdog")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self, bot_id: int) -> dict | None:
        return self.health.get(bot_id)

    def all_status(self) -> dict[int, dict]:
        return dict(self.health)

    # ---------------------------------------------------------------- 主循环
    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - 看门狗自己不能死
                    log.warning("掉线探测异常：%s: %s", type(exc).__name__, exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def run_once(self) -> None:
        async with session_scope() as session:
            bots = (
                await session.execute(
                    select(BotConnection).where(BotConnection.enabled == True)  # noqa: E712
                )
            ).scalars().all()
            engines = {
                engine.bot_connection_id: engine
                for engine in (await session.execute(select(HostedEngine))).scalars().all()
            }
            config = await load_app_config(session)

        for bot in bots:
            try:
                await self._check(
                    bot,
                    engines.get(bot.id),
                    interval=config.bot_health_interval_sec,
                    grace=config.bot_offline_grace_sec,
                    threshold=config.bot_good_fail_threshold,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                log.warning("检查 %s 掉线状态失败：%s", bot.name, exc)

    async def _check(
        self,
        bot: BotConnection,
        engine: HostedEngine | None,
        *,
        interval: int,
        grace: int,
        threshold: int,
    ) -> None:
        now = time.time()
        issues: list[str] = []
        client = registry.manager.peek(bot.id)
        health: dict = {
            "bot_id": bot.id,
            "name": bot.name,
            "mode": bot.mode,
            "transport": bot.transport,
            "state": bot.state,
            "checked_at": int(now),
            "issues": issues,
        }

        if client is None:
            health["state"] = bot.state or "no_client"
            if bot.mode in ("builtin", "external", "hosted"):
                issues.append("协议端未连接")
        else:
            info = client.health()
            health.update(
                {
                    "state": client.state,
                    "connected": client.connected,
                    "self_id": client.self_id,
                    "nickname": client.nickname,
                    "last_error": client.last_error,
                    "heartbeat_age": info.get("heartbeat_age"),
                    "heartbeat_online": info.get("heartbeat_online"),
                    "heartbeat_good": info.get("heartbeat_good"),
                    "last_notice": info.get("last_notice") or {},
                }
            )
            if not client.connected:
                issues.append("连接已断开")

            # 真实往返：能拿到登录信息说明协议层还活着（NapCat 的假在线靠这个戳穿）
            if client.connected:
                try:
                    info_login = await asyncio.wait_for(client.get_login_info(), timeout=5)
                    health["login_probe_ok"] = bool(info_login)
                    if not info_login:
                        issues.append("get_login_info 返回空")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    health["login_probe_ok"] = False
                    issues.append(f"get_login_info 失败：{type(exc).__name__}")

            heartbeat_age = info.get("heartbeat_age")
            hb_interval = info.get("heartbeat_interval") or 30000
            try:
                limit = max(90.0, float(hb_interval) / 1000.0 * 3)
            except (TypeError, ValueError):
                limit = 90.0
            if heartbeat_age is not None and heartbeat_age > limit:
                issues.append(f"心跳已 {int(heartbeat_age)} 秒未更新")

            if info.get("heartbeat_online") is False:
                issues.append("心跳 online=false")

            if info.get("heartbeat_good") is False:
                self._good_fail[bot.id] = self._good_fail.get(bot.id, 0) + 1
                if self._good_fail[bot.id] >= max(1, threshold):
                    issues.append(f"心跳 good=false 连续 {self._good_fail[bot.id]} 次")
            else:
                self._good_fail[bot.id] = 0

            notice = health.get("last_notice") or {}
            notice_type = str(notice.get("notice_type") or "")
            sub_type = str(notice.get("sub_type") or "")
            if notice_type in ("bot_status", "bot_offline") or sub_type in ("offline", "disconnect"):
                issues.append("收到协议端下线通知")

        # 托管容器：非 running 或日志里有被踢痕迹
        if engine is not None and engine.container_name:
            health["container"] = engine.container_name
            docker = hosted_manager.docker
            if docker is None:
                docker = hosted_manager._docker()  # noqa: SLF001 - 懒加载同一实例
            if docker is not None:
                try:
                    state = await docker.state(engine.container_name)
                except Exception:
                    state = ""
                health["container_state"] = state
                if state and state != "running":
                    issues.append(f"容器状态 {state}")
                if state == "running":
                    hits = await self._scan_logs(docker, engine.container_name)
                    health["container_hits"] = hits
                    if hits:
                        issues.append(f"容器日志出现下线关键词：{hits}")

        # 连续异常达到宽限期才告警，避免一次抖动就打扰
        if issues:
            since = self._suspect_since.setdefault(bot.id, now)
            health["suspect_seconds"] = int(now - since)
            if now - since >= grace:
                last = self._last_warn.get(bot.id, 0.0)
                if now - last >= WARN_GAP:
                    self._last_warn[bot.id] = now
                    self._last_alert_at[bot.id] = now
                    log.warning(
                        "QQ 账号疑似掉线（%s / %s）：%s",
                        bot.name,
                        bot.mode,
                        "；".join(issues),
                    )
        else:
            if bot.id in self._suspect_since:
                log.info("QQ 账号恢复正常：%s", bot.name)
            self._suspect_since.pop(bot.id, None)
            self._last_warn.pop(bot.id, None)

        health["last_alert_at"] = int(self._last_alert_at.get(bot.id, 0))
        health["healthy"] = not issues
        self.health[bot.id] = health

    async def _scan_logs(self, docker, container: str) -> str:
        try:
            text = await docker.logs(container, tail=60)
        except Exception:
            return ""
        hits = [word for word in LOG_KEYWORDS if word in text]
        return ",".join(hits)


watchdog = BotWatchdog()
