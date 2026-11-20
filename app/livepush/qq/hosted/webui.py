"""读取托管协议端 WebUI 的登录令牌。

NapCat 的令牌文件在容器内 `/app/napcat/config/webui.json`，而我们把
`<workdir>/config` 挂载到了同一路径（见 `napcat.py`），所以宿主机可以直接读；
读不到再退回 `docker exec cat`，最后才从启动日志里正则捞（日志只有 300 行窗口）。

令牌不写日志，只在内存缓存 10 秒，避免每次刷新页面都 exec 一次容器。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("livepush.hosted")

TOKEN_RE = re.compile(r"[\"']?token[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.:\-]{6,})", re.I)
CONFIG_NAME = "webui.json"
CONTAINER_PATH = "/app/napcat/config/webui.json"
CACHE_TTL = 10.0

_cache: dict[str, tuple[float, "WebUIToken"]] = {}


@dataclass(slots=True)
class WebUIToken:
    token: str = ""
    source: str = ""  # config / exec / logs / ""
    port: int = 0

    @property
    def found(self) -> bool:
        return bool(self.token)

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "source": self.source, "port": self.port}


def token_from_text(text: str) -> str:
    """从 JSON 或日志文本里捞令牌；只认带 token 标记的串。"""
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        value = data.get("token")
        if isinstance(value, str) and value.strip():
            return value.strip()
    match = TOKEN_RE.search(text)
    return match.group(1) if match else ""


def port_from_text(text: str, default: int = 0) -> int:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        try:
            value = int(data.get("port") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return default


def _read_local(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


async def read_webui_token(
    *,
    workdir: Path | None,
    container: str = "",
    docker: Any = None,
    log_tail: str = "",
    host_port: int = 0,
    cache_key: str = "",
) -> WebUIToken:
    """三层回退读取 WebUI 令牌；都失败返回空结果（UI 显示「需手动登录」）。"""
    key = cache_key or f"{workdir}|{container}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    result = WebUIToken(port=host_port)

    text = ""
    if workdir is not None:
        text = _read_local(Path(workdir) / "config" / CONFIG_NAME)
        if text:
            result.source = "config"
    if not text and docker is not None and container:
        text = await docker.exec_capture(container, "cat", CONTAINER_PATH)
        if text:
            result.source = "exec"
    if text:
        result.token = token_from_text(text)
        # 端口以宿主机映射为准；只有没给映射端口时才用配置里的容器内端口
        if not host_port:
            result.port = port_from_text(text, 0)

    if not result.token and log_tail:
        result.token = token_from_text(log_tail)
        if result.token:
            result.source = "logs"

    if not result.token:
        result.source = ""
    _cache[key] = (now, result)
    return result
