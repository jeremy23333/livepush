"""受管子进程（签名服务 / QQ 协议端）的统一进程管理。

为什么要自己管：内置模式下我们负责拉起协议端、读它的二维码、过期时换一张新码。
在 POSIX 上用 PTY 起子进程，能拿到终端里的块字符二维码；Windows 上退化为管道。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..util.logging import child_logger
from .qr import strip_ansi

IS_POSIX = os.name == "posix"


@dataclass
class ProcessOutput:
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=800))

    def append(self, line: str) -> None:
        self.lines.append(line)

    def tail(self, n: int = 120) -> list[str]:
        return list(self.lines)[-n:]

    def search(self, markers: tuple[str, ...], window: int = 200) -> str | None:
        recent = list(self.lines)[-window:]
        for line in reversed(recent):
            for marker in markers:
                if marker and marker in line:
                    return marker
        return None


class ManagedProcess:
    """一个长期子进程：启动、停止、重启、收集输出。"""

    def __init__(
        self,
        name: str,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.extra_env = env or {}
        self.output = ProcessOutput()
        self._logger = child_logger(name, log_path) if log_path else None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._buf = ""
        self._master_fd: int | None = None
        self.exit_code: int | None = None
        self.started_at: int = 0
        self.cwd.mkdir(parents=True, exist_ok=True)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.extra_env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("TERM", "xterm-256color")
        return env

    async def start(self) -> None:
        if self.running:
            return
        import time

        self.started_at = int(time.time())
        self.output.lines.clear()
        self._buf = ""
        if IS_POSIX:
            await self._start_pty()
        else:
            await self._start_pipe()

    async def _start_pty(self) -> None:
        import pty

        master, slave = pty.openpty()
        self._master_fd = master
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(self.cwd),
            env=self._env(),
            start_new_session=True,
        )
        os.close(slave)
        self._reader_task = asyncio.create_task(self._read_pty(master), name=f"pty-{self.name}")

    async def _read_pty(self, master: int) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def on_readable() -> None:
            try:
                data = os.read(master, 8192)
            except OSError:
                data = b""
            if data:
                queue.put_nowait(data)
            else:
                try:
                    loop.remove_reader(master)
                except Exception:
                    pass
                queue.put_nowait(None)

        try:
            loop.add_reader(master, on_readable)
            while True:
                data = await queue.get()
                if data is None:
                    break
                self._feed(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            try:
                loop.remove_reader(master)
            except Exception:
                pass
            try:
                os.close(master)
            except OSError:
                pass

    async def _start_pipe(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.cwd),
            env=self._env(),
        )
        self._reader_task = asyncio.create_task(self._read_pipe(), name=f"pipe-{self.name}")

    async def _read_pipe(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                self._feed(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _feed(self, data: bytes) -> None:
        text = data.decode("utf-8", "replace")
        self._buf += text
        # PTY 用 \r\n 与 \r 混排，统一按行切
        parts = re.split(r"\r\n|\r|\n", self._buf)
        self._buf = parts.pop() if parts else ""
        for raw in parts:
            line = strip_ansi(raw).rstrip()
            if not line:
                continue
            self.output.append(line)
            if self._logger is not None:
                self._logger.info(line)

    async def stop(self, timeout: float = 8.0) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        self.exit_code = proc.returncode if proc is not None else None
        self._proc = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def tail_text(self, n: int = 80) -> str:
        return "\n".join(self.output.tail(n))


def tail_lines(text: str, n: int = 80) -> str:
    return "\n".join(text.splitlines()[-n:])


def python_command(*args: str) -> list[str]:
    return [sys.executable, *args]
