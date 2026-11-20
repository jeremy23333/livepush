"""内存日志环形缓冲。

Web UI 的「实时日志」面板读这里，而不是去 tail 文件：
- `install()` 把 `BufferHandler` 挂到 root logger，与文件日志并存；
- 缓冲区只保留最近 `maxlen` 条，满了丢最旧，不会吃内存；
- 每条带自增 `seq`，SSE 用它做「只发增量」。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

#: 功能分区：按 logger 前缀归类，方便按模块过滤
AREA_RULES: list[tuple[str, str]] = [
    ("livepush.monitor", "采集"),
    ("livepush.push", "推送"),
    ("livepush.qq.hosted", "托管"),
    ("livepush.hosted", "托管"),
    ("livepush.login", "登录"),
    ("livepush.signer", "签名"),
    ("livepush.wrapper", "签名"),
    ("livepush.onebot11", "QQ 传输"),
    ("livepush.milky", "QQ 传输"),
    ("livepush.qq", "QQ"),
    ("livepush.api", "Web API"),
    ("livepush.web", "Web API"),
    ("livepush.child", "子进程"),
    ("uvicorn", "服务"),
    ("livepush", "服务"),
]

AREA_NAMES = ["采集", "推送", "登录", "签名", "QQ", "QQ 传输", "托管", "Web API", "子进程", "服务", "其他"]


def area_of(logger_name: str) -> str:
    for prefix, area in AREA_RULES:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return area
    return "其他"


class LogBuffer:
    """线程安全的日志环形缓冲（logging 可能在任意线程写）。"""

    def __init__(self, maxlen: int = 1000) -> None:
        self.maxlen = maxlen
        self._lines: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def add(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # pragma: no cover - 极端情况下别让日志系统崩
                record.exc_text = ""
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        item = {
            "seq": 0,
            "ts": record.created,
            "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "name": record.name,
            "area": area_of(record.name),
            "message": message,
        }
        with self._lock:
            self._seq += 1
            item["seq"] = self._seq
            self._lines.append(item)

    def snapshot(self, level: str = "", limit: int = 300) -> list[dict]:
        threshold = LEVELS.get(level.upper(), 0)
        limit = max(1, min(limit, self.maxlen))
        with self._lock:
            items = list(self._lines)
        if threshold:
            items = [item for item in items if LEVELS.get(item["level"], 0) >= threshold]
        return items[-limit:]

    def since(self, seq: int, level: str = "", limit: int = 200) -> list[dict]:
        threshold = LEVELS.get(level.upper(), 0)
        with self._lock:
            items = [item for item in self._lines if item["seq"] > seq]
        if threshold:
            items = [item for item in items if LEVELS.get(item["level"], 0) >= threshold]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


buffer = LogBuffer()


class BufferHandler(logging.Handler):
    """把日志塞进内存缓冲；出错也绝不能抛，避免影响业务。"""

    def __init__(self, target: LogBuffer | None = None) -> None:
        super().__init__()
        self.target = target or buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.add(record)
        except Exception:  # pragma: no cover
            pass


def install(target: LogBuffer | None = None, level: int = logging.INFO) -> BufferHandler:
    """挂到 root logger（幂等：重复调用只会加一个 handler）。"""
    buf = target or buffer
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, BufferHandler) and handler.target is buf:
            return handler
    handler = BufferHandler(buf)
    handler.setLevel(level)
    root.addHandler(handler)
    return handler


def get_buffer() -> LogBuffer:
    return buffer
