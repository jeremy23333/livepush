"""SnowLuma（备份镜像版）后端的托管细节。

SnowLuma 不自己登录：它注入一个**已经登录的 QQ 进程**，从 QQ 的本地登录库里读账号，
然后对外提供 OneBot11 + WebUI（5099）+ 远程桌面（6081）。所以托管它只需要把容器
拉起来，登录与 OneBot 连接配置都在它自己的 WebUI 里完成，我们只负责探测端口。
"""

from __future__ import annotations

from pathlib import Path

from .base import Artifact, HostedBackend, HostedContext, arch_arm64

VERSION = "2.4.2"
RELEASE = "https://github.com/XingYunStar/snowluma/releases/download/v2.4.2"

DOCKER_AMD64 = Artifact(
    name="SnowLuma-v2.4.2-docker-amd64.tar",
    url=f"{RELEASE}/SnowLuma-v2.4.2-docker-amd64.tar",
    sha256="2c84c9da24e44f75d8f3f7d6ca1289f748fd0ddb0fd33367b27b5a76f91ff5d1",
    size=736514560,
    version=VERSION,
)
DOCKER_ARM64 = Artifact(
    name="SnowLuma-v2.4.2-docker-arm64.tar",
    url=f"{RELEASE}/SnowLuma-v2.4.2-docker-arm64.tar",
    sha256="3142343b7c57481f5bbe8ea4222c6ef6e5e07a829d37295edc50b8959820dd14",
    size=724937216,
    version=VERSION,
)


class SnowLumaBackend(HostedBackend):
    key = "snowluma"
    title = "SnowLuma（备份镜像版）"
    description = "注入已登录的 QQ 进程、免扫码；自带 WebUI 与远程桌面，OneBot 连接在它页面里配。"
    artifact_version = VERSION
    ws_container_port = 3001
    http_container_port = 3000
    webui_container_port = 5099
    remote_container_port = 6081
    login_kind = "internal"

    def artifact(self) -> Artifact:
        return DOCKER_ARM64 if arch_arm64() else DOCKER_AMD64

    def docker_args(self, ctx: HostedContext) -> list[str]:
        return [
            "-d",
            "--name",
            ctx.container_name,
            "--restart",
            "unless-stopped",
            "--shm-size=1g",
            "--ulimit",
            "nofile=65536:1048576",
            "--cap-add=SYS_PTRACE",
            "--security-opt",
            "seccomp=unconfined",
            "-e",
            "SNOWLUMA_WEBUI_HOST=0.0.0.0",
            "-e",
            f"SNOWLUMA_WEBUI_PORT={self.webui_container_port}",
            "-e",
            'SNOWLUMA_QQ_FLAGS=--disable-gpu --disable-software-rasterizer --disable-gpu-compositing',
            "-e",
            "TZ=Asia/Shanghai",
            "-p",
            f"127.0.0.1:{ctx.ports.webui}:{self.webui_container_port}",
            "-p",
            f"127.0.0.1:{ctx.ports.remote}:{self.remote_container_port}",
            "-p",
            f"127.0.0.1:{ctx.ports.ws}:{self.ws_container_port}",
            "-p",
            f"127.0.0.1:{ctx.ports.http}:{self.http_container_port}",
            "-v",
            f"{ctx.workdir / 'data'}:/app/data",
            "-v",
            f"{ctx.workdir / 'config'}:/app/.config",
            "-v",
            f"{ctx.workdir / 'local'}:/app/.local/share",
        ]

    def write_config(self, ctx: HostedContext) -> list[Path]:
        # SnowLuma 的配置只存在它自己的 WebUI/数据卷里，没有可生成的配置文件。
        for name in ("data", "config", "local"):
            (ctx.workdir / name).mkdir(parents=True, exist_ok=True)
        return []

    def qr_scan_dirs(self, ctx: HostedContext) -> list[Path]:
        return [ctx.workdir / "data", ctx.workdir / "config", ctx.workdir]

    def notes(self) -> list[str]:
        return [
            "SnowLuma 不扫码：它注入容器里已经登录的 QQ 进程来识别账号。",
            "首次使用：打开「远程桌面」在里面的 QQ 上扫码登录，再到「WebUI」里添加 OneBot11 连接"
            "（HTTP/WS 端口 3000/3001，Token 自己填，或留空后由我们探测）。",
            "WebUI 初始密码在容器日志里，可用下面的「查看日志」找「远程桌面密码 / initial credentials」。",
            "许可证为源码可见非商业许可；镜像来自第三方归档备份，QQ 升级后可能失效。",
        ]

    def webui_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/" if port else ""

    def remote_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/" if port else ""
