"""签名服务体检。

自建签名服务器（ntqq-sign-server）暴露：
- `GET /health`            → {status, uptime_seconds, platform, version}
- `GET /api/sign/appinfo`  → 完整 BotAppInfo（拿版本做匹配校验）
- `POST /`                 → 老式签名接口，body {cmd, seq, src}

这里只做「能不能用 / 版本对不对」的判断，并把错误翻译成可操作的中文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass(slots=True)
class SignCheck:
    ok: bool
    health: str  # ok | unreachable | unauthorized | mismatch | unknown
    message: str = ""
    platform: str = ""
    version: str = ""
    expected: str = ""
    detail: dict = field(default_factory=dict)


def normalize_base(base_url: str) -> str:
    base = (base_url or "").strip()
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    if not base.endswith("/"):
        base += "/"
    return base


async def check_sign(
    base_url: str,
    token: str = "",
    proxy: str = "",
    expected_version: str = "",
    timeout: float = 8.0,
) -> SignCheck:
    base = normalize_base(base_url)
    if not base:
        return SignCheck(ok=False, health="unknown", message="未配置签名服务器地址")

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs: dict = {"headers": headers, "timeout": timeout, "follow_redirects": True}
    if proxy:
        kwargs["proxy"] = proxy

    detail: dict = {"base": base}
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            # 1) 健康检查
            try:
                health_resp = await client.get(base + "health")
            except httpx.HTTPError as exc:
                return SignCheck(
                    ok=False,
                    health="unreachable",
                    message=f"连不上签名服务器：{type(exc).__name__}。检查地址/端口/防火墙，或代理设置。",
                    detail={**detail, "error": str(exc)},
                )
            if health_resp.status_code in (401, 403):
                return SignCheck(
                    ok=False,
                    health="unauthorized",
                    message="签名服务器要求鉴权（401/403）。填上 Token，或确认这个地址是否需要 Bearer。",
                    detail={**detail, "status": health_resp.status_code},
                )
            if health_resp.status_code == 404:
                return SignCheck(
                    ok=False,
                    health="unreachable",
                    message="签名服务器返回 404：地址可能不对，或该服务没有 /health（不是 ntqq-sign-server）。",
                    detail={**detail, "status": 404},
                )
            health_json: dict = {}
            try:
                health_json = health_resp.json()
            except ValueError:
                health_json = {}
            detail["health"] = health_json

            # 2) appinfo（版本匹配）
            appinfo: dict = {}
            try:
                app_resp = await client.get(base + "api/sign/appinfo")
                if app_resp.status_code < 400:
                    appinfo = app_resp.json()
            except (httpx.HTTPError, ValueError):
                appinfo = {}
            detail["appinfo"] = {k: appinfo.get(k) for k in ("Os", "CurrentVersion", "Kernel", "AppClientVersion") if isinstance(appinfo, dict)}

            version = ""
            if isinstance(appinfo, dict):
                version = str(appinfo.get("CurrentVersion") or appinfo.get("version") or "")
            if not version:
                version = str(health_json.get("version") or "")
            platform = str(health_json.get("platform") or (appinfo.get("Os") if isinstance(appinfo, dict) else "") or "")

            if expected_version and version and expected_version != version and expected_version != "fetched":
                return SignCheck(
                    ok=False,
                    health="mismatch",
                    message=f"协议版本不匹配：协议端要 {expected_version}，签名服务给 {version}。改协议版本或换签名服务。",
                    platform=platform,
                    version=version,
                    expected=expected_version,
                    detail=detail,
                )

            return SignCheck(
                ok=True,
                health="ok",
                message=f"签名服务可用（{platform or '未知平台'} / {version or '版本未知'}）。",
                platform=platform,
                version=version,
                expected=expected_version,
                detail=detail,
            )
    except Exception as exc:  # 兜底，绝不让体检把页面打崩
        return SignCheck(ok=False, health="unknown", message=f"体检异常：{type(exc).__name__}: {exc}", detail=detail)


def diagnose_login_code(code: int) -> str:
    """把登录失败码翻译成人话。"""
    mapping = {
        45: "签名不可用或协议版本不匹配（Code 45）。先跑签名体检：确认签名服务可达、且协议版本与之一致。",
        237: "设备锁/验证码校验失败（Code 237）。按协议端提示在终端完成验证，或改用短信验证码登录。",
        100: "参数错误（Code 100），通常是 QQ 号或配置有问题。",
    }
    return mapping.get(code, f"登录失败（Code {code}）。看协议端日志尾部定位原因。")
