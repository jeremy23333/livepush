"""托管协议端的生命周期编排。

一次「安装」= 下载镜像 tar → `docker load` → 分配本机端口 → 写 OneBot11 配置 →
`docker run` → 等到 `get_login_info` 能通 → 把 BotConnection 指向 `ws://127.0.0.1:<port>`。

安装是长任务，所以用内存 Job 跑在后台，Web UI 轮询进度；job 丢失不影响已装好的容器
（容器与端口都落在 `hosted_engine` 表里，重启后由 `resume_all()` 恢复）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ...appconfig import clean_base_url, get_setting, load_app_config, set_setting
from ...config import runtime
from ...db import session_scope
from ...models import BotConnection, HostedEngine, now_ts
from .. import registry
from .base import (
    HostPorts,
    HostedBackend,
    HostedContext,
    container_name_for,
)
from .discover import probe_onebot11, wait_for_onebot11
from .docker import Docker, DockerError
from .download import DownloadError, fetch
from .napcat import NapCatBackend
from .snowluma import SnowLumaBackend
from .webui import WebUIToken, read_webui_token

log = logging.getLogger("livepush.hosted")

BACKENDS: dict[str, HostedBackend] = {
    b.key: b for b in (NapCatBackend(), SnowLumaBackend())
}

PORT_START = 30000
PORT_SPAN = 4000
PORTS_PER_INSTANCE = 4

# 逐容器的 WebUI 公网地址，存 app_setting（JSON：{"<bot_id>": "https://..."}）。
# 不放进 hosted_engine 表，是为了避免给 SQLite 加列（本项目没有迁移框架）。
PUBLIC_SETTING_KEY = "hosted_webui_public"


class HostedError(RuntimeError):
    pass


def allocate_ports(count: int = PORTS_PER_INSTANCE, start: int = PORT_START) -> int:
    """找一段连续空闲端口，返回起始端口。"""
    for base in range(start, start + PORT_SPAN - count):
        free = True
        for port in range(base, base + count):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    free = False
                    break
        if free:
            return base
    raise HostedError("找不到空闲端口（30000-34000 都被占用了？）")


@dataclass
class InstallJob:
    id: str
    bot_id: int
    backend: str = ""
    stage: str = "queued"
    message: str = "排队中"
    downloaded: int = 0
    total: int = 0
    ok: bool = False
    done: bool = False
    error: str = ""
    logs: list[str] = field(default_factory=list)
    created_at: int = field(default_factory=now_ts)

    def say(self, message: str) -> None:
        self.logs.append(message)
        del self.logs[:-300]

    def progress(self, downloaded: int, total: int) -> None:
        self.downloaded = downloaded
        if total:
            self.total = total

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bot_id": self.bot_id,
            "backend": self.backend,
            "stage": self.stage,
            "message": self.message,
            "downloaded": self.downloaded,
            "total": self.total,
            "ok": self.ok,
            "done": self.done,
            "error": self.error,
            "logs": self.logs[-80:],
            "created_at": self.created_at,
        }


class HostedManager:
    # 容器启动 + QQ 初始化可能很慢，给足时间；测试里会调小
    PROBE_TIMEOUT = 180.0
    START_PROBE_TIMEOUT = 120.0

    def __init__(self) -> None:
        self.jobs: dict[str, InstallJob] = {}
        self.docker: Docker | None = None
        self.probe = probe_onebot11
        self._tasks: set[asyncio.Task] = set()

    # ---- 基础工具 ----
    def _docker(self) -> Docker:
        if self.docker is None:
            self.docker = Docker(runtime.settings.docker_bin)
        return self.docker

    def backends(self) -> list[dict[str, Any]]:
        return [
            {
                "key": b.key,
                "title": b.title,
                "description": b.description,
                "version": b.artifact_version,
                "notes": b.notes(),
                "supports_native": b.supports_native,
                "login_kind": b.login_kind,
            }
            for b in BACKENDS.values()
        ]

    def get_job(self, job_id: str) -> InstallJob | None:
        return self.jobs.get(job_id)

    def job_for_bot(self, bot_id: int) -> InstallJob | None:
        for job in reversed(list(self.jobs.values())):
            if job.bot_id == bot_id:
                return job
        return None

    # ---- DB 读写（用 dict 快照，避免 ORM 对象被会话关闭后过期） ----
    @staticmethod
    def _engine_dict(engine: HostedEngine) -> dict[str, Any]:
        return {
            "id": engine.id,
            "bot_connection_id": engine.bot_connection_id,
            "backend": engine.backend,
            "runtime": engine.runtime,
            "image_ref": engine.image_ref,
            "container_name": engine.container_name,
            "install_dir": engine.install_dir,
            "artifact_path": engine.artifact_path,
            "artifact_version": engine.artifact_version,
            "host_ws_port": engine.host_ws_port,
            "host_http_port": engine.host_http_port,
            "host_webui_port": engine.host_webui_port,
            "remote_port": engine.remote_port,
            "token": engine.token,
            "state": engine.state,
            "last_error": engine.last_error,
        }

    async def _load_bot(self, bot_id: int) -> dict[str, Any]:
        async with session_scope() as session:
            bot = await session.get(BotConnection, bot_id)
            if bot is None:
                raise HostedError(f"连接 {bot_id} 不存在")
            return {
                "id": bot.id,
                "name": bot.name,
                "mode": bot.mode,
                "transport": bot.transport,
                "backend": bot.backend,
                "uin": bot.uin,
                "token": bot.token,
                "ws_url": bot.ws_url,
            }

    async def _patch_bot(self, bot_id: int, **fields: Any) -> None:
        async with session_scope() as session:
            bot = await session.get(BotConnection, bot_id)
            if bot is None:
                return
            for key, value in fields.items():
                setattr(bot, key, value)
            bot.updated_at = now_ts()

    async def _load_engine(self, bot_id: int) -> dict[str, Any] | None:
        async with session_scope() as session:
            engine = (
                await session.execute(
                    select(HostedEngine).where(HostedEngine.bot_connection_id == bot_id)
                )
            ).scalar_one_or_none()
            return self._engine_dict(engine) if engine else None

    async def _patch_engine(self, bot_id: int, **fields: Any) -> None:
        async with session_scope() as session:
            engine = (
                await session.execute(
                    select(HostedEngine).where(HostedEngine.bot_connection_id == bot_id)
                )
            ).scalar_one_or_none()
            if engine is None:
                return
            for key, value in fields.items():
                setattr(engine, key, value)
            engine.updated_at = now_ts()

    async def _ensure_engine(self, bot_id: int, backend: HostedBackend) -> dict[str, Any]:
        async with session_scope() as session:
            engine = (
                await session.execute(
                    select(HostedEngine).where(HostedEngine.bot_connection_id == bot_id)
                )
            ).scalar_one_or_none()
            if engine is None:
                base = allocate_ports()
                engine = HostedEngine(
                    bot_connection_id=bot_id,
                    backend=backend.key,
                    runtime="docker",
                    container_name=container_name_for(backend.key, bot_id),
                    install_dir=str(runtime.settings.hosted_dir / f"{backend.key}-{bot_id}"),
                    artifact_version=backend.artifact_version,
                    host_ws_port=base + 0,
                    host_http_port=base + 1,
                    host_webui_port=base + 2,
                    remote_port=base + 3,
                    token=secrets.token_urlsafe(24),
                    state="pending",
                )
                session.add(engine)
                await session.flush()
            return self._engine_dict(engine)

    # ---- 对外操作 ----
    def install(self, bot_id: int) -> InstallJob:
        existing = self.job_for_bot(bot_id)
        if existing and not existing.done:
            return existing
        job = InstallJob(id=uuid.uuid4().hex[:12], bot_id=bot_id)
        self.jobs[job.id] = job
        task = asyncio.create_task(self._install(job), name=f"hosted-install-{bot_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _install(self, job: InstallJob) -> None:
        bot_id = job.bot_id
        try:
            bot = await self._load_bot(bot_id)
            if bot["mode"] != "hosted":
                raise HostedError("这条连接不是「托管」模式，不能安装容器")
            backend = BACKENDS.get(bot["backend"])
            if backend is None:
                raise HostedError(f"未知的托管后端：{bot['backend']}")
            job.backend = backend.key
            job.stage = "prepare"
            job.message = "准备目录与端口"
            engine = await self._ensure_engine(bot_id, backend)
            workdir = Path(engine["install_dir"])
            for sub in ("artifacts", "config", "cache", "data", "local"):
                (workdir / sub).mkdir(parents=True, exist_ok=True)
            job.say(f"工作目录：{workdir}")

            docker = self._docker()
            version = await docker.available()
            if not version:
                raise HostedError(
                    "找不到可用的 Docker。请先在服务器上安装 Docker 并确保当前用户能执行 docker 命令；"
                    "本项目不会自动安装 Docker。"
                )
            job.say(f"Docker 服务端 {version}")

            artifact = backend.artifact()
            dest = workdir / "artifacts" / artifact.name
            job.stage = "artifact"
            job.message = f"下载 {artifact.name}"
            job.total = artifact.size
            try:
                await fetch(
                    artifact,
                    dest,
                    mirror=runtime.settings.artifact_mirror,
                    progress=job.progress,
                    log_fn=job.say,
                )
            except DownloadError as exc:
                raise HostedError(
                    f"{exc}。可以手动下载后放到 {dest}，再点一次「安装/启动」。"
                ) from exc

            job.stage = "load"
            job.message = "docker load 载入镜像"
            image = engine["image_ref"] or ""
            loaded = await docker.load(dest)
            if loaded:
                image = loaded
            if not image:
                raise HostedError("docker load 完成了，但没有发现新镜像；请检查 tar 是否完整。")
            job.say(f"镜像：{image}")

            if not engine["host_ws_port"]:
                base = allocate_ports()
                await self._patch_engine(
                    bot_id,
                    host_ws_port=base,
                    host_http_port=base + 1,
                    host_webui_port=base + 2,
                    remote_port=base + 3,
                )
                engine = await self._load_engine(bot_id) or engine

            ports = HostPorts(
                ws=engine["host_ws_port"],
                http=engine["host_http_port"],
                webui=engine["host_webui_port"],
                remote=engine["remote_port"],
            )
            ctx = HostedContext(
                bot_id=bot_id,
                uin=int(bot["uin"] or 0),
                workdir=workdir,
                token=engine["token"],
                ports=ports,
                container_name=engine["container_name"],
                image_ref=image,
            )
            backend.write_config(ctx)
            job.stage = "run"
            job.message = "启动容器"
            if await docker.state(engine["container_name"]):
                await docker.rm(engine["container_name"])
            await docker.run(backend.docker_args(ctx), image)
            await self._patch_engine(
                bot_id,
                image_ref=image,
                state="running",
                last_error="",
                artifact_path=str(dest),
                artifact_version=artifact.version,
            )
            job.say(f"容器已启动：{engine['container_name']}")

            job.stage = "probe"
            job.message = "等待协议端 OneBot11 端口就绪（首启要等 QQ 加载）"
            found = await wait_for_onebot11(
                [f"ws://127.0.0.1:{ports.ws}"],
                engine["token"],
                timeout=self.PROBE_TIMEOUT,
            )
            if not found:
                raise HostedError(
                    "容器在跑，但 OneBot11 端口没有响应。可能是首次启动还没就绪，或镜像内 QQ 版本过期；"
                    "点「查看日志」看容器输出，稍后可点「连接」重试。"
                )
            ws_url, info = found
            job.say(f"探测到 OneBot11：{ws_url}（{info.get('nickname') or info.get('self_id')}）")
            job.stage = "attach"
            job.message = "绑定连接"
            await self._bind(bot_id, engine, ws_url, info)
            job.stage = "done"
            job.message = "完成"
            job.ok = True
            job.done = True
        except Exception as exc:  # noqa: BLE001 - 安装任务必须把失败落成状态
            log.exception("托管安装失败 bot=%s", bot_id)
            job.error = str(exc)
            job.stage = "failed"
            job.message = f"失败：{exc}"
            job.done = True
            job.ok = False
            job.say(f"[FAIL] {exc}")
            with contextlib.suppress(Exception):
                await self._patch_engine(bot_id, state="error", last_error=str(exc))

    async def _bind(self, bot_id: int, engine: dict[str, Any], ws_url: str, info: dict[str, Any]) -> None:
        await self._patch_bot(
            bot_id,
            transport="onebot11",
            ws_url=ws_url,
            token=engine["token"],
            self_id=int(info.get("self_id") or 0),
            nickname=str(info.get("nickname") or ""),
            state="connected",
            last_error="",
        )
        await self._patch_engine(bot_id, state="running", last_error="")
        with contextlib.suppress(Exception):
            await registry.manager.stop(bot_id)
            async with session_scope() as session:
                bot = await session.get(BotConnection, bot_id)
                if bot is not None:
                    await registry.manager.get(bot)

    async def attach(self, bot_id: int, ws_url: str = "", token: str = "") -> dict[str, Any]:
        engine = await self._load_engine(bot_id)
        if engine is None:
            raise HostedError("这条连接还没有安装过托管容器")
        url = ws_url or f"ws://127.0.0.1:{engine['host_ws_port']}"
        info = await self.probe(url, token or engine["token"], timeout=8.0)
        if not info:
            raise HostedError(f"连不上 {url}，确认容器在跑、Token 正确、端口映射没被占用")
        await self._bind(bot_id, engine, url, info)
        return {"ws_url": url, **info}

    async def start(self, bot_id: int) -> dict[str, Any]:
        engine = await self._load_engine(bot_id)
        if engine is None:
            job = self.install(bot_id)
            return {"job_id": job.id}
        docker = self._docker()
        state = await docker.state(engine["container_name"])
        if not state:
            raise HostedError("容器不存在（可能被删了）；请重新点「安装/启动」")
        if state != "running":
            await docker.start(engine["container_name"])
            await self._patch_engine(bot_id, state="running", last_error="")
        info = await wait_for_onebot11(
            [f"ws://127.0.0.1:{engine['host_ws_port']}"],
            engine["token"],
            timeout=self.START_PROBE_TIMEOUT,
        )
        if not info:
            raise HostedError("容器已启动，但 OneBot11 端口还没就绪；稍后点「连接」重试")
        ws_url, probe = info
        await self._bind(bot_id, engine, ws_url, probe)
        return {"ws_url": ws_url, **probe}

    async def stop(self, bot_id: int) -> dict[str, Any]:
        engine = await self._load_engine(bot_id)
        if engine is None:
            raise HostedError("这条连接还没有托管容器")
        await self._docker().stop(engine["container_name"])
        await self._patch_engine(bot_id, state="stopped")
        await self._patch_bot(bot_id, state="stopped")
        with contextlib.suppress(Exception):
            await registry.manager.stop(bot_id)
        return {"state": "stopped"}

    async def remove(self, bot_id: int) -> dict[str, Any]:
        engine = await self._load_engine(bot_id)
        if engine is not None:
            with contextlib.suppress(DockerError):
                await self._docker().rm(engine["container_name"])
            async with session_scope() as session:
                row = (
                    await session.execute(
                        select(HostedEngine).where(HostedEngine.bot_connection_id == bot_id)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
        await self._patch_bot(bot_id, ws_url="", state="stopped", last_error="")
        with contextlib.suppress(Exception):
            await registry.manager.stop(bot_id)
        return {"state": "removed"}

    # -------------------------------------------------- WebUI 公网地址（Cloudflare Tunnel 等）
    async def _public_override(self, bot_id: int) -> str:
        async with session_scope() as session:
            raw = await get_setting(session, PUBLIC_SETTING_KEY, "")
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return ""
        if not isinstance(data, dict):
            return ""
        return clean_base_url(str(data.get(str(bot_id)) or ""))

    async def public_webui_url(self, bot_id: int) -> str:
        """「带令牌打开」用的公网 WebUI 地址；空串表示没配置（退回内嵌/局域网地址）。

        优先逐容器覆盖，其次设置页里的全局默认；base 已带路径就原样用，否则补后端路径
        （NapCat 是 `/webui`，SnowLuma 是根路径）。
        """
        engine = await self._load_engine(bot_id)
        if engine is None:
            return ""
        base = await self._public_override(bot_id)
        if not base:
            async with session_scope() as session:
                config = await load_app_config(session)
            base = clean_base_url(config.hosted_webui_public_base)
        if not base:
            return ""
        backend = BACKENDS.get(engine["backend"])
        path = getattr(backend, "webui_path", "") if backend else ""
        if path and not base.endswith(path):
            base = base + path
        return base

    async def set_public_url(self, bot_id: int, url: str) -> str:
        """保存/清除某条托管连接的公网地址，返回规范化后实际生效的地址。"""
        clean = clean_base_url(url)
        async with session_scope() as session:
            raw = await get_setting(session, PUBLIC_SETTING_KEY, "")
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            key = str(bot_id)
            if clean:
                data[key] = clean
            else:
                data.pop(key, None)
            await set_setting(session, PUBLIC_SETTING_KEY, json.dumps(data, ensure_ascii=False))
        return await self.public_webui_url(bot_id)

    async def status(self, bot_id: int, host: str = "127.0.0.1") -> dict[str, Any]:
        engine = await self._load_engine(bot_id)
        if engine is None:
            return {"installed": False, "state": "missing", "notes": []}
        backend = BACKENDS.get(engine["backend"])
        docker = self._docker()
        state = await docker.state(engine["container_name"])
        docker_up = await docker.available()
        logs = await docker.logs(engine["container_name"], 300) if state else ""
        ports = HostPorts(
            ws=engine["host_ws_port"],
            http=engine["host_http_port"],
            webui=engine["host_webui_port"],
            remote=engine["remote_port"],
        )
        # WebUI 登录令牌：宿主机 <workdir>/config/webui.json → docker exec → 启动日志
        webui = WebUIToken(port=ports.webui)
        if state == "running" and ports.webui:
            with contextlib.suppress(Exception):
                webui = await read_webui_token(
                    workdir=Path(engine["install_dir"]) if engine["install_dir"] else None,
                    container=engine["container_name"],
                    docker=docker,
                    log_tail=logs,
                    host_port=ports.webui,
                    cache_key=engine["container_name"],
                )
        return {
            "installed": True,
            "backend": engine["backend"],
            "container": engine["container_name"],
            "state": state or ("docker-missing" if not docker_up else "missing"),
            "image_ref": engine["image_ref"],
            "artifact": {"path": engine["artifact_path"], "version": engine["artifact_version"]},
            "install_dir": engine["install_dir"],
            "ports": {"ws": ports.ws, "http": ports.http, "webui": ports.webui, "remote": ports.remote},
            "ws_url": f"ws://{host}:{ports.ws}" if ports.ws else "",
            "webui_url": backend.webui_url(host, ports.webui) if backend else "",
            "webui_public_url": await self.public_webui_url(bot_id),
            "remote_url": backend.remote_url(host, ports.remote) if backend else "",
            "webui": webui.to_dict(),
            "token": engine["token"],
            "log_tail": logs,
            "last_error": engine["last_error"],
            "notes": backend.notes() if backend else [],
        }

    async def webui_token(self, bot_id: int) -> dict[str, Any]:
        """轻量读取 WebUI 令牌（不拉容器日志，供内嵌页 iframe 用）。"""
        engine = await self._load_engine(bot_id)
        if engine is None:
            return {"token": "", "source": "", "port": 0}
        info = await read_webui_token(
            workdir=Path(engine["install_dir"]) if engine["install_dir"] else None,
            container=engine["container_name"],
            host_port=engine["host_webui_port"],
            cache_key=engine["container_name"],
        )
        return info.to_dict()

    async def stop_all(self) -> None:
        """进程退出时只停容器，不删（下次启动还能 resume）。"""
        for task in list(self._tasks):
            task.cancel()
        async with session_scope() as session:
            rows = (await session.execute(select(HostedEngine))).scalars().all()
            names = [row.container_name for row in rows if row.state == "running"]
        for name in names:
            with contextlib.suppress(Exception):
                await self._docker().stop(name)

    async def resume_all(self) -> None:
        """启动时把已经装好的托管连接恢复起来（失败只记日志，不阻塞启动）。"""
        async with session_scope() as session:
            rows = (await session.execute(select(HostedEngine))).scalars().all()
            engines = [self._engine_dict(row) for row in rows]
        docker = self._docker()
        if not engines or not await docker.available():
            return
        for engine in engines:
            bot_id = engine["bot_connection_id"]
            try:
                state = await docker.state(engine["container_name"])
                if state == "exited":
                    await docker.start(engine["container_name"])
                if state in ("running", "exited"):
                    info = await self.probe(
                        f"ws://127.0.0.1:{engine['host_ws_port']}", engine["token"], timeout=8.0
                    )
                    if info:
                        await self._bind(bot_id, engine, f"ws://127.0.0.1:{engine['host_ws_port']}", info)
            except Exception as exc:  # noqa: BLE001
                log.warning("恢复托管连接 %s 失败：%s", bot_id, exc)


hosted_manager = HostedManager()


def backend_of(key: str) -> HostedBackend | None:
    return BACKENDS.get(key)
