"""故障告警：把关键 WARNING/ERROR 推送到企业微信群机器人。

为什么要自己再收一遍：企业微信群机器人限 20 条/分钟、markdown 正文 ≤ 4096 字节，
监控类程序一旦出问题（比如 B 站封 IP）会瞬间刷屏，所以这里做三层收敛：
1. 只收 >= 配置等级的日志；
2. 同一「模块 + 归一化消息」在冷却期内只推一次（数字会被替换成 #，避免每次不同的 ID）；
3. 每分钟最多推 N 条，超出记 suppressed，不排队堆积。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

#: 告警自己打日志时不再进告警队列，避免死循环
SELF_LOGGER = "livepush.alerts"


def normalize(message: str) -> str:
    """把易变的部分抹掉，让「同一类错误」能命中同一个冷却键。"""
    # 先用占位符吃掉十六进制（否则后面的 \d+ 会把 0x 里的 0 也替换掉），
    # 再抹掉普通数字，最后把占位符还原成统一的 0x#。
    text = re.sub(r"0x[0-9a-fA-F]+", "\x00", message)
    text = re.sub(r"\d+", "#", text)
    text = text.replace("\x00", "0x#")
    return text[:120]


class AlertManager:
    def __init__(self) -> None:
        self.enabled = False
        self.url = ""
        self.min_level = "ERROR"
        self.cooldown_sec = 600
        self.max_per_minute = 10

        self.sent = 0
        self.suppressed = 0
        self.last_error = ""
        self.last_sent_at = 0.0
        self.last_errcode: int | None = None
        self.last_elapsed_ms = 0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None
        self._last: dict[str, float] = {}
        self._window: list[float] = []
        self._client: httpx.AsyncClient | None = None

    # ---------------------------------------------------------------- 配置/生命周期
    def configure(self, *, enabled: bool, url: str, min_level: str, cooldown_sec: int,
                  max_per_minute: int) -> None:
        self.url = (url or "").strip()
        self.enabled = bool(enabled and self.url)
        self.min_level = (min_level or "ERROR").upper()
        self.cooldown_sec = max(0, int(cooldown_sec))
        self.max_per_minute = max(1, int(max_per_minute))

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        loop = loop or asyncio.get_running_loop()
        self._loop = loop
        if self._task is None or self._task.done():
            self._queue = asyncio.Queue(maxsize=200)
            self._task = loop.create_task(self._run(), name="alert-manager")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- 入口（任意线程）
    def submit(self, record: logging.LogRecord, text: str) -> None:
        if not self.enabled or record.name == SELF_LOGGER:
            return
        if LEVELS.get(record.levelname, 0) < LEVELS.get(self.min_level, 40):
            return
        loop = self._loop
        if loop is None or loop.is_closed() or self._queue is None:
            return
        key = f"{record.name}|{normalize(text)}"
        try:
            loop.call_soon_threadsafe(self._enqueue, (record.levelname, text, key))
        except RuntimeError:  # pragma: no cover - 事件循环正在关闭
            pass

    def _enqueue(self, item: tuple[str, str, str]) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.suppressed += 1

    # ---------------------------------------------------------------- 发送循环
    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            level, text, key = await self._queue.get()
            try:
                await self._handle(level, text, key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底，别让循环退出
                self.last_error = f"{type(exc).__name__}: {exc}"

    async def _handle(self, level: str, text: str, key: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        if last and now - last < self.cooldown_sec:
            self.suppressed += 1
            return
        self._window = [stamp for stamp in self._window if now - stamp < 60]
        if len(self._window) >= self.max_per_minute:
            self.suppressed += 1
            return
        self._last[key] = now
        self._window.append(now)
        if len(self._last) > 500:  # 防止长时间运行后 key 无限增长
            self._last = {k: v for k, v in self._last.items() if now - v < max(self.cooldown_sec, 3600)}
        ok = await self._send(level, text)
        if ok:
            self.sent += 1
            self.last_sent_at = time.time()

    async def _send(self, level: str, text: str) -> bool:
        if not self.url:
            self.last_error = "还没有配置企业微信 webhook 地址"
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0, trust_env=False)
        started = time.perf_counter()
        self.last_errcode = None
        snippet = text if len(text) <= 3000 else text[:3000] + "\n…（已截断）"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        content = (
            f"**livepush 告警**\n"
            f"> 等级：{level}\n"
            f"> 时间：{stamp}\n"
            f"> 冷却：同类 {self.cooldown_sec} 秒内只推一次\n\n"
            f"{snippet}"
        )
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            resp = await self._client.post(self.url, json=payload)
            data = resp.json() if resp.content else {}
        except httpx.HTTPError as exc:
            self.last_elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.last_error = f"网络错误：{exc}"
            logging.getLogger(SELF_LOGGER).warning("告警推送失败：%s", exc)
            return False
        except ValueError:
            self.last_elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.last_error = "企业微信返回的不是 JSON"
            return False
        self.last_elapsed_ms = int((time.perf_counter() - started) * 1000)
        errcode = data.get("errcode", 0)
        self.last_errcode = errcode if isinstance(errcode, int) else None
        if errcode not in (0, None):
            self.last_error = f"errcode={errcode} {data.get('errmsg', '')}"
            logging.getLogger(SELF_LOGGER).warning("告警被企业微信拒绝：%s", self.last_error)
            return False
        self.last_error = ""
        return True

    async def send_test(self) -> dict:
        if not self.url:
            return {
                "ok": False,
                "configured": False,
                "error": "还没有配置企业微信 webhook 地址",
            }
        ok = await self._send(
            "TEST", "这是一条测试告警：如果你在群里看到它，说明 webhook 配置正确。"
        )
        if ok:
            self.sent += 1
            self.last_sent_at = time.time()
        return {
            "ok": ok,
            "configured": True,
            "error": self.last_error,
            "errcode": self.last_errcode,
            "elapsed_ms": self.last_elapsed_ms,
        }

    def status(self) -> dict[str, Any]:
        masked = ""
        if self.url:
            masked = self.url[: self.url.rfind("/") + 1] + "…" if "/" in self.url else "…"
        return {
            "enabled": self.enabled,
            "configured": bool(self.url),
            "url": masked,
            "min_level": self.min_level,
            "cooldown_sec": self.cooldown_sec,
            "max_per_minute": self.max_per_minute,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
            "last_errcode": self.last_errcode,
            "queued": self._queue.qsize() if self._queue is not None else 0,
        }


class AlertLogHandler(logging.Handler):
    """挂在 root logger 上：把达到级别的日志交给 AlertManager（不阻塞业务线程）。"""

    def __init__(self, manager: AlertManager) -> None:
        super().__init__()
        self.manager = manager
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.manager.submit(record, record.getMessage())
        except Exception:  # pragma: no cover
            pass


alert_manager = AlertManager()


def install(manager: AlertManager | None = None) -> AlertLogHandler:
    mgr = manager or alert_manager
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, AlertLogHandler) and handler.manager is mgr:
            return handler
    handler = AlertLogHandler(mgr)
    handler.setLevel(logging.WARNING)
    root.addHandler(handler)
    return handler


def configure_alerts(config) -> None:
    """从 AppConfig 同步告警设置（启动时、保存设置时各调一次）。"""
    alert_manager.configure(
        enabled=config.alert_enabled,
        url=config.alert_wecom_url,
        min_level=config.alert_min_level,
        cooldown_sec=config.alert_cooldown_sec,
        max_per_minute=config.alert_max_per_minute,
    )


install_alerts = install
