"""自建签名服务的进程管理（本地回环，无 Token）。

它就是一个 Python 脚本 + 官方 QQ 的 wrapper.node：
    python3 sign.py --wrapper /path/wrapper.node --host 127.0.0.1 --port 8080
依赖见 scripts/bootstrap.sh —— 正是 ntqq-sign-server 的 Dockerfile 里的那套系统库。

wrapper.node 不再要求必须放在 `data/sign/`：它会沿「显式路径 → 环境变量 →
data/sign → 常见安装目录」查找；知道 QQ 版本时还会主动带上 `--offset`，
带偏移起不来再退回让 sign.py 自己探测。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
from pathlib import Path

import httpx

from ..config import runtime
from .supervisor import ManagedProcess
from .wrapper import find_wrapper, known_offset, qq_dir_of, read_qq_version

log = logging.getLogger("livepush.signer.server")


class SignServer:
    def __init__(self) -> None:
        self._process: ManagedProcess | None = None
        self.last_error = ""
        self._wrapper: Path | None = None

    @property
    def dir(self) -> Path:
        return runtime.settings.sign_dir

    @property
    def script(self) -> Path:
        return self.dir / "sign.py"

    @property
    def wrapper(self) -> Path:
        """当前实际会用的 wrapper.node（找不到时退回 data/sign 下的默认路径，便于提示）。"""
        return self._wrapper or (self.dir / "wrapper.node")

    @property
    def port(self) -> int:
        return runtime.settings.sign_port

    @property
    def base_url(self) -> str:
        return f"http://{runtime.settings.sign_host}:{self.port}/"

    @property
    def installed(self) -> bool:
        return self.script.is_file() and find_wrapper() is not None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.running

    def _commands(self, wrapper: Path) -> list[list[str]]:
        base = [
            sys.executable,
            str(self.script),
            "--wrapper",
            str(wrapper),
            "--host",
            runtime.settings.sign_host,
            "--port",
            str(self.port),
        ]
        qq_dir = qq_dir_of(wrapper)
        if qq_dir:
            base.extend(["--qq-dir", qq_dir])
        base.extend(shlex.split(runtime.settings.sign_extra_args or ""))
        offset = known_offset(read_qq_version(wrapper))
        commands = []
        if offset and "--offset" not in base:
            commands.append([*base, "--offset", hex(offset)])
        commands.append(base)  # 兜底：让 sign.py 自己探测偏移
        return commands

    async def start(self) -> dict:
        if self.running:
            return {"ok": True, "message": "签名服务已在运行"}
        if not self.script.is_file():
            self.last_error = f"缺少签名脚本：{self.script}。先跑 scripts/bootstrap.sh，或手动放置。"
            return {"ok": False, "message": self.last_error}

        wrapper = find_wrapper()
        if wrapper is None:
            self.last_error = (
                "找不到 QQ 的 wrapper.node。可以用 scripts/bootstrap.sh --with-sign 自动下载并解包，"
                "或手动放置到 data/sign/wrapper.node，或用环境变量 LIVEPUSH_WRAPPER_NODE 指路。"
            )
            return {"ok": False, "message": self.last_error}
        self._wrapper = wrapper

        last_tail = ""
        for index, cmd in enumerate(self._commands(wrapper)):
            self._process = ManagedProcess(
                name="sign",
                cmd=cmd,
                cwd=self.dir,
                log_path=runtime.settings.logs_dir / "sign.log",
            )
            await self._process.start()
            if await self.wait_healthy(timeout=25):
                self.last_error = ""
                note = "（含 --offset）" if "--offset" in cmd else ""
                return {"ok": True, "message": f"签名服务已启动{note}：{self.base_url}"}
            last_tail = self._process.tail_text(20)
            await self._process.stop()
            self._process = None
            if index == 0:
                log.warning("签名服务带偏移启动失败，改为自动探测重试：%s", last_tail[-300:])

        self.last_error = f"签名服务 25 秒内 /health 未通过。日志尾部：\n{last_tail}"
        return {"ok": False, "message": self.last_error}

    async def wait_healthy(self, timeout: float = 25.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(timeout=3.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self._process is not None and not self._process.running:
                    return False
                try:
                    resp = await client.get(self.base_url + "health")
                    if resp.status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1.0)
        return False

    async def stop(self) -> dict:
        if self._process is not None:
            await self._process.stop()
            self._process = None
        return {"ok": True, "message": "签名服务已停止"}

    def status(self) -> dict:
        wrapper = find_wrapper()
        version = read_qq_version(wrapper) if wrapper else ""
        return {
            "installed": self.installed,
            "running": self.running,
            "base_url": self.base_url,
            "script": str(self.script),
            "wrapper": str(wrapper or self.wrapper),
            "qq_version": version,
            "known_offset": hex(known_offset(version)) if known_offset(version) else "",
            "last_error": self.last_error,
            "log": self._process.tail_text(30) if self._process else "",
        }

    def log_tail(self, n: int = 60) -> str:
        return self._process.tail_text(n) if self._process else ""


sign_server = SignServer()
