"""内置协议端的后端描述（配置模板 + 日志标记 + 启动命令）。

⚠️ M0 探测项：下面这些键名与日志标记来自各项目文档/源码，实际版本可能不同。
首次在目标机跑通后请按实际输出校正——改正这里即可，不影响其余代码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class LaunchContext:
    uin: int
    http_host: str = "127.0.0.1"
    http_port: int = 3000
    access_token: str = ""
    webhook_url: str = ""
    sign_url: str = ""
    sign_token: str = ""
    sign_proxy: str = ""
    protocol_os: str = "Linux"
    protocol_version: str = "fetched"


@dataclass(slots=True)
class BackendSpec:
    key: str
    label: str
    requires_sign: bool
    binary_names: tuple[str, ...]
    config_name: str
    qr_markers: tuple[str, ...]
    success_markers: tuple[str, ...]
    expired_markers: tuple[str, ...]
    keystore_globs: tuple[str, ...]
    extra_args: tuple[str, ...] = ()
    notes: str = ""
    _writer: str = field(default="", repr=False)

    def binary_path(self, protocol_root: Path) -> Path | None:
        for name in self.binary_names:
            candidate = protocol_root / name
            if candidate.is_file():
                return candidate
        return None

    def build_command(self, binary: Path) -> list[str]:
        return [str(binary), *self.extra_args]

    def write_config(self, workdir: Path, ctx: LaunchContext) -> Path:
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / self.config_name
        if self._writer == "yogurt":
            payload = yogurt_config(ctx)
        else:
            payload = lagrange_config(ctx)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


def yogurt_config(ctx: LaunchContext) -> dict:
    return {
        "configVersion": 3,
        "protocol": {
            "uin": ctx.uin,
            "password": "",
            "os": ctx.protocol_os,
            "version": ctx.protocol_version,
            "signApiUrl": ctx.sign_url,
            # 留空表示走「老式 sign API」（自建签名服务器即此模式）；
            # 填了 token 就切到 V2 sec-sign，那时 version 不能是 fetched。
            "pcLagrangeSignToken": ctx.sign_token,
            "androidUseLegacySign": False,
            "autoRenewSession": True,
        },
        "milky": {
            "http": {
                "host": ctx.http_host,
                "port": ctx.http_port,
                "prefix": "",
                "accessToken": ctx.access_token,
                "corsOrigins": [],
            },
            "webhook": {"endpoints": [ctx.webhook_url] if ctx.webhook_url else []},
            "reportSelfMessage": True,
            "preloadContacts": False,
            "ffmpegPath": "",
        },
        "logging": {"ansiLevel": "ANSI256", "coreLogLevel": "INFO"},
        "security": {"skipOnLaunchListenAddressCheck": True},
    }


def lagrange_config(ctx: LaunchContext) -> dict:
    return {
        "Lagrange": {
            "Login": {"Uin": ctx.uin, "Password": None, "AutoReLogin": True},
            "Protocol": {
                "Signer": {
                    "BaseUrl": ctx.sign_url or "https://sign.lagrangecore.org/api/",
                    "Token": ctx.sign_token,
                    "ProxyUrl": ctx.sign_proxy or None,
                }
            },
            "QrCode": {"Compatible": False},
            "Server": {"AutoReconnect": True, "GetOptimumServer": True},
            "Log": {"Level": "Information"},
        },
        "Milky": {
            "AccessToken": ctx.access_token,
            "HttpServer": {"Host": ctx.http_host, "Port": ctx.http_port},
            "Api": {"Http": {"Enabled": True}},
            "Event": {
                "WebHook": {
                    "Enabled": bool(ctx.webhook_url),
                    "TargetUrls": [ctx.webhook_url] if ctx.webhook_url else [],
                }
            },
        },
    }


YOGURT = BackendSpec(
    key="yogurt",
    label="Yogurt（acidify-core，Milky）",
    requires_sign=True,
    binary_names=("yogurt", "yogurt.exe", "yogurt-linux-x64/yogurt"),
    config_name="config.json",
    # sealdice 观测到的标记：二维码文件已保存 / 已上线 / 二维码已过期
    qr_markers=("二维码文件已保存", "Fetch QrCode Success", "请使用手机QQ扫码", "扫码"),
    success_markers=("已上线", "successfully logged in", "登录成功", "Login success"),
    expired_markers=("二维码已过期", "CodeExpired", "QrCode State: CodeExpired"),
    keystore_globs=("session-store.json", "session-store-android.json"),
    notes="PC 协议扫码登录；signApiUrl 必填，自建签名服务即可免 Token。",
    _writer="yogurt",
)

LAGRANGE_MILKY = BackendSpec(
    key="lagrange_milky",
    label="Lagrange.Milky（Lagrange.Core V2，Milky）",
    requires_sign=True,
    binary_names=("Lagrange.Milky", "Lagrange.Milky.exe", "lagrange-milky/Lagrange.Milky"),
    config_name="appsettings.json",
    qr_markers=("Fetch QrCode Success", "QR Code", "Please scan", "扫码"),
    success_markers=("successfully logged in", "Bot online", "登录成功"),
    expired_markers=("QrCode State: CodeExpired", "CodeExpired", "二维码已过期"),
    keystore_globs=("*.keystore", "keystore.json"),
    extra_args=("shell",),
    notes="默认 Signer.BaseUrl 公共地址已不可用，需指向自建签名服务。",
    _writer="lagrange",
)

BACKENDS: dict[str, BackendSpec] = {
    YOGURT.key: YOGURT,
    LAGRANGE_MILKY.key: LAGRANGE_MILKY,
}


def get_backend(key: str) -> BackendSpec | None:
    return BACKENDS.get(key)
