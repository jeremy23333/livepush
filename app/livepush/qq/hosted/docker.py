"""薄薄的 Docker CLI 封装（只用 `docker` 命令，不引入 docker SDK 依赖）。

用 `asyncio.create_subprocess_exec` 直接调命令行：面板要跑在用户的 Ubuntu 上，
`docker` 命令本身是他自己装好的，我们只做 load/run/start/stop/rm/logs/inspect。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path

log = logging.getLogger("livepush.hosted.docker")


class DockerError(RuntimeError):
    pass


class Docker:
    def __init__(self, bin_spec: str = "docker", *, default_timeout: float = 120.0) -> None:
        self._cmd = shlex.split(bin_spec) or ["docker"]
        self.default_timeout = default_timeout
        self.last_error = ""

    async def _run(
        self, *args: str, timeout: float | None = None, check: bool = True
    ) -> tuple[int, str, str]:
        cmd = [*self._cmd, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout or self.default_timeout)
            code = proc.returncode or 0
        except FileNotFoundError as exc:
            self.last_error = f"找不到命令：{' '.join(self._cmd)}"
            raise DockerError(self.last_error) from exc
        except asyncio.TimeoutError as exc:
            self.last_error = f"docker {' '.join(args)} 超时"
            raise DockerError(self.last_error) from exc
        out = out_b.decode("utf-8", "replace").strip()
        err = err_b.decode("utf-8", "replace").strip()
        if check and code != 0:
            self.last_error = err or out or f"docker {' '.join(args)} 退出码 {code}"
            raise DockerError(self.last_error)
        return code, out, err

    async def available(self) -> str:
        """返回服务端版本号；不可用时返回空串（不抛异常，供 UI/doctor 判断）。"""
        try:
            code, out, _ = await self._run("version", "--format", "{{.Server.Version}}", timeout=15, check=False)
        except DockerError:
            return ""
        return out.strip() if code == 0 else ""

    async def images(self) -> set[str]:
        try:
            _, out, _ = await self._run("images", "--format", "{{.Repository}}:{{.Tag}}", timeout=30, check=False)
        except DockerError:
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    async def load(self, path: Path) -> str:
        """`docker load -i`，返回新出现的镜像引用（失败抛 DockerError）。"""
        before = await self.images()
        _, out, err = await self._run("load", "-i", str(path), timeout=900)
        after = await self.images()
        fresh = sorted(after - before)
        if fresh:
            return fresh[0]
        for line in (out + "\n" + err).splitlines():
            if line.lower().startswith("loaded image"):
                return line.split(":", 1)[1].strip()
        return ""

    async def run(self, args: list[str], image: str) -> str:
        _, out, _ = await self._run("run", *args, image, timeout=180)
        return out.strip()

    async def start(self, name: str) -> None:
        await self._run("start", name, timeout=120)

    async def stop(self, name: str) -> None:
        code, _, err = await self._run("stop", name, timeout=60, check=False)
        if code != 0 and "No such container" not in err:
            raise DockerError(err or f"停止容器 {name} 失败")

    async def rm(self, name: str) -> None:
        code, _, err = await self._run("rm", "-f", name, timeout=60, check=False)
        if code != 0 and "No such container" not in err:
            raise DockerError(err or f"删除容器 {name} 失败")

    async def state(self, name: str) -> str:
        if not name:
            return ""
        try:
            code, out, _ = await self._run(
                "inspect", "--format", "{{.State.Status}}", name, timeout=20, check=False
            )
        except DockerError:
            return ""
        return out.strip() if code == 0 else ""

    async def logs(self, name: str, tail: int = 80) -> str:
        if not name:
            return ""
        try:
            _, out, err = await self._run("logs", "--tail", str(tail), name, timeout=30, check=False)
        except DockerError:
            return ""
        return "\n".join(part for part in (out, err) if part)
