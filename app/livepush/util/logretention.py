"""日志文件清单与保留策略。

`data/logs/` 下有四类日志（见 docs/OPERATIONS.md）：

- ``app.log``（+ 轮转备份）：应用主日志，已由 RotatingFileHandler 控制体积；
- ``service.log``：systemd 的 stdout/stderr，没有轮转，靠启动时截断兜底；
- ``sign.log``：自建签名服务子进程输出；
- ``<backend>-<id>.log``：内置协议端子进程输出。

本模块负责：列出文件、按「保留 N 天」删除旧文件、计算下一次执行时间。
删除时**不碰活动文件**（app.log / service.log），避免删掉正在写的 inode。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..appconfig import AppConfig, get_setting, load_app_config, set_setting

log = logging.getLogger("livepush.logs")

STATE_KEY = "log_cleanup"
DAY = 86400

#: 被进程长期持有、不能删除的日志文件
PROTECTED_NAMES = {"app.log", "service.log"}

_AREA_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^app\.log"), "应用主日志"),
    (re.compile(r"^service\.log$"), "服务进程"),
    (re.compile(r"^sign\.log"), "签名服务"),
    (re.compile(r"^[a-z_]+-\d+\.log"), "内置协议端"),
]


def area_of(name: str) -> str:
    for pattern, label in _AREA_RULES:
        if pattern.match(name):
            return label
    return "其他"


def log_files(logs_dir: Path) -> list[Path]:
    """列出日志目录下的一层文件（含 ``.log.1`` 轮转备份），跳过子目录与符号链接。"""
    result: list[Path] = []
    try:
        entries = list(logs_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return result
    for path in entries:
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        if path.name.startswith("."):
            continue
        if ".log" not in path.name:
            continue
        result.append(path)
    return sorted(result, key=lambda p: p.name)


def list_log_files(logs_dir: Path, *, now: int | None = None) -> list[dict]:
    now = int(now if now is not None else time.time())
    out: list[dict] = []
    for path in log_files(logs_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "age_days": round(max(0, now - stat.st_mtime) / DAY, 1),
                "area": area_of(path.name),
                "protected": path.name in PROTECTED_NAMES,
            }
        )
    return out


def cleanup_logs(
    days: int,
    *,
    logs_dir: Path,
    now: int | None = None,
    dry_run: bool = False,
) -> dict:
    """删除 ``mtime`` 早于 ``days`` 天的日志文件。

    ``days <= 0`` 表示关闭自动清理。返回本次执行的报告 dict。
    """
    now = int(now if now is not None else time.time())
    report: dict = {
        "at": now,
        "days": int(days),
        "enabled": int(days) > 0,
        "deleted": [],
        "freed_bytes": 0,
        "kept": 0,
        "skipped": [],
        "errors": [],
    }
    if int(days) <= 0:
        report["kept"] = len(log_files(logs_dir))
        return report

    cutoff = now - int(days) * DAY
    for path in log_files(logs_dir):
        if path.name in PROTECTED_NAMES:
            report["skipped"].append(path.name)
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            report["errors"].append(f"{path.name}: {exc}")
            continue
        if stat.st_mtime >= cutoff:
            report["kept"] += 1
            continue
        if dry_run:
            report["deleted"].append(path.name)
            report["freed_bytes"] += stat.st_size
            continue
        try:
            size = stat.st_size
            path.unlink()
        except OSError as exc:
            report["errors"].append(f"{path.name}: {exc}")
            continue
        report["deleted"].append(path.name)
        report["freed_bytes"] += size
    return report


def truncate_service_log(path: Path, max_mb: int) -> int:
    """启动时把超限的 service.log 截断，返回截断掉的字节数（0 表示未处理）。

    systemd 用 ``StandardOutput=append:`` 打开文件后一直持有 fd，截断是安全的：
    后续写入会从文件末尾（0）继续，磁盘空间立即释放。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if max_mb <= 0 or size <= max_mb * 1024 * 1024:
        return 0
    try:
        with path.open("w", encoding="utf-8"):
            pass
    except OSError as exc:
        log.warning("截断 %s 失败：%s", path, exc)
        return 0
    log.info("已截断 %s（原 %s MB）", path, round(size / 1024 / 1024, 1))
    return size


def next_run_at(now: float, hour: int) -> int:
    """下一次「每天 hour 点」的时间戳（本地时区）。"""
    current = datetime.fromtimestamp(now)
    target = current.replace(hour=max(0, min(int(hour), 23)), minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return int(target.timestamp())


class LogRetentionTask:
    """每天在 ``log_cleanup_hour`` 执行一次日志清理；启动时也先跑一次。"""

    def __init__(self, logs_dir: Path, store_key: str = STATE_KEY) -> None:
        self.logs_dir = logs_dir
        self.store_key = store_key
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._state: dict = {"at": 0, "next_at": 0, "report": {}, "error": ""}

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._state["next_at"] = next_run_at(time.time(), (await self._config()).log_cleanup_hour)
        self._task = asyncio.create_task(self._loop(), name="livepush-log-retention")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ 执行
    async def _config(self) -> AppConfig:
        from ..db import session_scope

        async with session_scope() as session:
            return await load_app_config(session)

    async def run_once(self, *, force: bool = False) -> dict:
        config = await self._config()
        days = config.log_retention_days
        report = cleanup_logs(days, logs_dir=self.logs_dir)
        self._state = {
            "at": int(time.time()),
            "next_at": next_run_at(time.time(), config.log_cleanup_hour),
            "report": report,
            "error": "",
        }
        try:
            from ..db import session_scope

            async with session_scope() as session:
                await set_setting(session, self.store_key, _json(self._state))
        except Exception:  # 持久化失败不影响清理本身
            log.debug("保存日志清理状态失败", exc_info=True)
        if report["enabled"]:
            log.info(
                "日志清理：删除 %d 个文件，释放 %s KB（保留 %d 个）",
                len(report["deleted"]),
                round(report["freed_bytes"] / 1024, 1),
                report["kept"],
            )
        return {**report, "next_at": self._state["next_at"]}

    async def _loop(self) -> None:
        # 启动后先跑一次
        try:
            await self.run_once()
        except Exception:
            log.exception("启动时清理日志失败")
        while not self._stop.is_set():
            config = await self._config()
            delay = max(60, next_run_at(time.time(), config.log_cleanup_hour) - int(time.time()))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                continue
            finally:
                self._state["next_at"] = next_run_at(time.time(), config.log_cleanup_hour)

    # ------------------------------------------------------------------ 状态
    def state(self) -> dict:
        return dict(self._state)

    async def load_state(self) -> dict:
        try:
            from ..db import session_scope

            async with session_scope() as session:
                raw = await get_setting(session, self.store_key, "")
            if raw:
                import json

                data = json.loads(raw)
                if isinstance(data, dict):
                    self._state.update(data)
        except Exception:
            pass
        return self.state()


def _json(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)


#: 进程内单例，供 API 查询/触发（启动时由 main 装配）
retention_task: "LogRetentionTask | None" = None


def setup_retention(logs_dir: Path) -> LogRetentionTask:
    global retention_task
    retention_task = LogRetentionTask(logs_dir)
    return retention_task
