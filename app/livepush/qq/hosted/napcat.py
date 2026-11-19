"""NapCat（备份镜像版）后端的托管细节。

上游 `XingYunStar/NapCatQQ` 是把 QQ 的 wrapper.node 当原生模块加载进 Electron
主进程再对外提供 OneBot11 的实现。它的 Docker 镜像自带 QQ 与 xvfb，所以我们只需要
`docker load` + 挂两个目录 + 让出 3000/3001/6099 三个端口。

注意：镜像里 WebUI 的登录令牌由 NapCat 自己生成在 `/app/napcat/config/webui.json`，
我们不覆盖它；二维码由它写到 `/app/napcat/cache/qrcode.png`，同时日志里也会打
可解码的二维码 URL，两条路都能取。
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import Artifact, HostPorts, HostedBackend, HostedContext, arch_arm64, host_uid_gid_args

VERSION = "4.18.42"
RELEASE = "https://github.com/XingYunStar/NapCatQQ/releases/download/v4.18.42"

DOCKER_AMD64 = Artifact(
    name="napcat-docker-v4.18.42-amd64.tar",
    url=f"{RELEASE}/napcat-docker-v4.18.42-amd64.tar",
    sha256="85e3fd02b58fd836a0177734b6ff77a896000bcfcf209b3b200d7e8b8452df7c",
    size=612216320,
    version=VERSION,
)
DOCKER_ARM64 = Artifact(
    name="napcat-docker-v4.18.42-arm64.tar",
    url=f"{RELEASE}/napcat-docker-v4.18.42-arm64.tar",
    sha256="0d14594844e9135c760e710754a8778d98649de2ca837fce5a61b69b7d39954d",
    size=597573632,
    version=VERSION,
)

# 无 Docker 时的后备：Linux 壳 + 用户自备 QQ（QQ 安装包需自己准备）。
SHELL_ZIP = Artifact(
    name="NapCat.Shell.zip",
    url=f"{RELEASE}/NapCat.Shell.zip",
    sha256="02e5ff0f79916c1e04c81c1b4590056406fb4293f1f1300c955e71fd08b2829d",
    size=35967555,
    version=VERSION,
    kind="zip",
)


def onebot11_config(token: str, *, http_port: int, ws_port: int) -> dict:
    """NapCat `onebot11_<uin>.json` 的完整结构（字段与它的 zod schema 对齐）。"""

    return {
        "network": {
            "httpServers": [
                {
                    "name": "livepush-http",
                    "enable": True,
                    "host": "0.0.0.0",
                    "port": http_port,
                    "messagePostFormat": "array",
                    "enableCors": True,
                    "token": token,
                }
            ],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [
                {
                    "name": "livepush-ws",
                    "enable": True,
                    "host": "0.0.0.0",
                    "port": ws_port,
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "token": token,
                    "enableForcePushEvent": True,
                    "debug": False,
                    "heartInterval": 30000,
                }
            ],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
        "timeout": {
            "baseTimeout": 10000,
            "uploadSpeedKBps": 256,
            "downloadSpeedKBps": 256,
            "maxTimeout": 1800000,
        },
    }


class NapCatBackend(HostedBackend):
    key = "napcat"
    title = "NapCat（备份镜像版）"
    description = "加载真实 QQ 客户端的 NTQQ 实现，免签名；用 Docker 一键起容器，扫码登录。"
    artifact_version = VERSION
    ws_container_port = 3001
    http_container_port = 3000
    webui_container_port = 6099
    login_kind = "qr"
    supports_native = True

    def artifact(self) -> Artifact:
        return DOCKER_ARM64 if arch_arm64() else DOCKER_AMD64

    def docker_args(self, ctx: HostedContext) -> list[str]:
        args = [
            "-d",
            "--name",
            ctx.container_name,
            "--restart",
            "unless-stopped",
            "-e",
            f"ACCOUNT={ctx.uin}",
            "-e",
            "WSR_ENABLE=true",
            *host_uid_gid_args(),
            "-p",
            f"127.0.0.1:{ctx.ports.ws}:{self.ws_container_port}",
            "-p",
            f"127.0.0.1:{ctx.ports.http}:{self.http_container_port}",
        ]
        if ctx.ports.webui:
            args += ["-p", f"127.0.0.1:{ctx.ports.webui}:{self.webui_container_port}"]
        args += [
            "-v",
            f"{ctx.workdir / 'config'}:/app/napcat/config",
            "-v",
            f"{ctx.workdir / 'cache'}:/app/napcat/cache",
        ]
        return args

    def write_config(self, ctx: HostedContext) -> list[Path]:
        config_dir = ctx.workdir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        data = onebot11_config(ctx.token, http_port=self.http_container_port, ws_port=self.ws_container_port)
        written: list[Path] = []
        # NapCat 的多账号配置文件名是 onebot11_<uin>.json；老版本读 onebot11.json。
        for name in (f"onebot11_{ctx.uin}.json", "onebot11.json"):
            path = config_dir / name
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(path)
        return written

    def qr_scan_dirs(self, ctx: HostedContext) -> list[Path]:
        return [ctx.workdir / "cache", ctx.workdir / "config", ctx.workdir]

    def notes(self) -> list[str]:
        return [
            "NapCat 镜像自带 QQ 与虚拟显示，首次启动会在容器日志里打印二维码链接；"
            "二维码文件落在 <工作目录>/cache/qrcode.png。",
            "WebUI 令牌在容器内 /app/napcat/config/webui.json，可用下面的「查看日志」定位登录状态。",
            "镜像由第三方归档备份项目发布，QQ 升级后可能需要换新镜像。",
        ]

    def webui_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/webui" if port else ""

    def remote_url(self, host: str, port: int) -> str:
        return ""


def hosted_ports(engine) -> HostPorts:  # 便于测试/序列化复用
    return HostPorts(
        ws=engine.host_ws_port,
        http=engine.host_http_port,
        webui=engine.host_webui_port,
        remote=engine.remote_port,
    )
