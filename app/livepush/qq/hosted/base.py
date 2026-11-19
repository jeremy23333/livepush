"""托管协议端的公共抽象。

一个"托管协议端"= 我们用 Docker 拉起的第三方 OneBot11 实现（NapCat / SnowLuma）。
我们只负责：下产物、载入镜像、分配本机端口、生成配置、起容器、探测端口、把
`BotConnection` 指向它。协议本身还是对方实现的，我们不碰。
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"[^a-z0-9_.-]+")


def container_name_for(backend: str, bot_id: int) -> str:
    return "livepush-" + NAME_RE.sub("-", backend.lower()) + f"-{bot_id}"


@dataclass(slots=True)
class Artifact:
    name: str
    url: str
    sha256: str
    size: int
    version: str = ""
    kind: str = "docker-tar"  # docker-tar | zip | deb


@dataclass(slots=True)
class HostPorts:
    ws: int = 0
    http: int = 0
    webui: int = 0
    remote: int = 0


@dataclass(slots=True)
class HostedContext:
    bot_id: int
    uin: int
    workdir: Path
    token: str
    ports: HostPorts
    container_name: str
    image_ref: str = ""


@dataclass(slots=True)
class HostedStatus:
    """给 Web UI 用的只读快照。"""

    backend: str = ""
    container: str = ""
    state: str = "unknown"  # running | exited | missing | docker-missing
    image_ref: str = ""
    install_dir: str = ""
    ws_url: str = ""
    webui_url: str = ""
    remote_url: str = ""
    token: str = ""
    log_tail: str = ""
    last_error: str = ""
    notes: list[str] = field(default_factory=list)


class HostedBackend(ABC):
    key: str = ""
    title: str = ""
    description: str = ""
    artifact_version: str = ""
    # 容器内的端口（我们把它映射到本机随机高位端口）
    ws_container_port: int = 3001
    http_container_port: int = 3000
    webui_container_port: int = 0
    remote_container_port: int = 0
    # 是否支持在没有 Docker 时用原生安装（目前只有 NapCat 设想）
    supports_native: bool = False
    # 登录方式：qr=出二维码 / internal=在它自己的界面里登录
    login_kind: str = "qr"

    @abstractmethod
    def artifact(self) -> Artifact:
        """当前架构对应的主产物（Docker 镜像 tar）。"""

    @abstractmethod
    def docker_args(self, ctx: HostedContext) -> list[str]:
        """`docker run` 中 image 之前的全部参数。"""

    @abstractmethod
    def write_config(self, ctx: HostedContext) -> list[Path]:
        """在容器启动前写好它需要的配置，返回写出的文件。"""

    def qr_scan_dirs(self, ctx: HostedContext) -> list[Path]:
        """二维码图片可能出现的位置（容器内目录已挂载到这些本机路径）。"""
        return [ctx.workdir / "cache", ctx.workdir / "config", ctx.workdir]

    def notes(self) -> list[str]:
        return []

    def webui_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/" if port else ""

    def remote_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/" if port else ""


def host_uid_gid_args() -> list[str]:
    """Linux 上让容器内文件属主与宿主机当前用户一致（NapCat 用得到）。"""
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return ["-e", f"NAPCAT_UID={os.getuid()}", "-e", f"NAPCAT_GID={os.getgid()}"]
    return []


def arch_arm64() -> bool:
    import platform

    machine = (platform.machine() or "").lower()
    return machine in ("aarch64", "arm64")
