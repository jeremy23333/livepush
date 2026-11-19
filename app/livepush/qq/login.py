"""内置协议端的扫码登录编排。

状态机：
    stopped → preparing → launching → generating_qr → qr_ready
            → logged_in
            └ 过期/超时 → expired → 自动重启出新码（≤N 次）
            └ 崩溃 / Code 45 → error
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from ..config import runtime
from ..db import session_scope
from ..models import BotConnection, LoginSession, SignProvider, now_ts
from . import registry
from .backends import BackendSpec, LaunchContext, get_backend
from .milky import MilkyClient
from .qr import QrCapture, detect_qr
from .signer import check_sign, diagnose_login_code
from .supervisor import ManagedProcess

log = logging.getLogger("livepush.login")

QR_TTL = 180  # 二维码有效期（秒），过期就换一张
POLL_INTERVAL = 1.0
REUSE_GRACE = 30  # 旧登录态最多验证多久，不成就回到扫码


def _free_port(preferred: int | None = None) -> int:
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class LoginFlow:
    conn_id: int
    uin: int
    spec: BackendSpec
    workdir: Path
    ctx: LaunchContext
    log_path: Path
    max_attempts: int = 3

    state: str = "stopped"
    error: str = ""
    attempts: int = 0
    qr: QrCapture | None = None
    qr_at: int = 0
    started_at: int = 0
    process: ManagedProcess | None = None
    reuse_at: int = 0
    reuse_failed: bool = False
    _observed: set = field(default_factory=set)

    def _build_process(self) -> ManagedProcess:
        binary = self.spec.binary_path(runtime.settings.protocol_dir / self.spec.key)
        if binary is None:
            searched = runtime.settings.protocol_dir / self.spec.key
            raise FileNotFoundError(
                f"找不到协议端可执行文件。请把它放到 {searched}/ 下（可用 scripts/bootstrap.sh 下载），"
                f"候选文件名：{', '.join(self.spec.binary_names)}"
            )
        return ManagedProcess(
            name=f"{self.spec.key}-{self.conn_id}",
            cmd=self.spec.build_command(binary),
            cwd=self.workdir,
            log_path=self.log_path,
        )

    async def launch(self) -> None:
        self.state = "preparing"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.spec.write_config(self.workdir, self.ctx)
        self.process = self._build_process()
        self.state = "launching"
        self.started_at = now_ts()
        await self.process.start()
        self.attempts += 1
        log.info("协议端已启动：%s（attempt=%s，pid=%s）", self.spec.key, self.attempts, self.process.pid)

    async def restart_for_new_qr(self) -> bool:
        if self.attempts >= self.max_attempts:
            self.state = "error"
            self.error = f"二维码已连续 {self.attempts} 次失效，已停止自动刷新。请查看协议端日志后手动重试。"
            await self.shutdown()
            return False
        self.qr = None
        self.qr_at = 0
        self._observed.clear()
        if self.process is not None:
            await self.process.stop()
        # 换端口避免旧监听未释放
        self.ctx.http_port = _free_port()
        await self.launch()
        return True

    async def shutdown(self) -> None:
        if self.process is not None:
            await self.process.stop()
        if self.state not in ("logged_in", "error"):
            self.state = "stopped" if self.state != "error" else "error"

    # ------------------------------------------------------------------ 轮询
    async def poll(self) -> None:
        if self.process is None:
            return
        if not self.process.running:
            if self.state not in ("logged_in", "error"):
                self.state = "error"
                self.error = f"协议端进程已退出（code={self.process.exit_code}）。看日志定位原因。"
            return

        lines = self.process.output.tail(200)
        self._scan_codes(lines)

        # 登录成功
        if self.process.output.search(self.spec.success_markers):
            self.state = "logged_in"
            self.error = ""
            return

        # 二维码过期
        if self.process.output.search(self.spec.expired_markers) and self.state != "logged_in":
            self.state = "expired"
            if await self.restart_for_new_qr():
                return
            return

        # 旧登录态：keystore 存在不代表还能发消息，必须用 get_login_info 确认；
        # 验证期间不覆盖二维码状态，超过 REUSE_GRACE 就回到扫码流程。
        if self.state == "reusing_session":
            if await self._confirm_session():
                self.state = "logged_in"
                self.error = ""
                return
            if now_ts() - self.reuse_at > REUSE_GRACE:
                self.reuse_failed = True
                self.state = "launching"
                self.error = "复用的登录态验证失败，改用二维码登录"
            return
        if self.qr is None and not self.reuse_failed:
            if self._has_keystore():
                self.state = "reusing_session"
                self.reuse_at = now_ts()
                return

        # 二维码就绪 / 正在等待扫码
        if self.qr is None:
            capture = detect_qr(self.workdir, lines)
            if capture is not None:
                if capture.sha1 != (self.qr.sha1 if self.qr else ""):
                    log.info("拿到二维码（%s）", capture.kind)
                self.qr = capture
                self.qr_at = now_ts()
                self.state = "qr_ready"
        elif self.state in ("launching", "generating_qr") or self.state == "qr_ready":
            # 文件可能被替换成新码
            capture = detect_qr(self.workdir, lines)
            if capture is not None and capture.sha1 != (self.qr.sha1 if self.qr else ""):
                self.qr = capture
                self.qr_at = now_ts()
                self.state = "qr_ready"

        # 二维码超时
        if self.state == "qr_ready" and self.qr_at and now_ts() - self.qr_at > QR_TTL:
            self.state = "expired"
            self.error = "二维码已过期，正在换一张新的"
            await self.restart_for_new_qr()
            return

        if self.state == "launching" and now_ts() - self.started_at > 90:
            self.state = "error"
            self.error = "启动 90 秒仍没有二维码。可能签名不可用（Code 45）或配置有误，请看协议端日志。"

    def _has_keystore(self) -> bool:
        """协议端的登录态文件是否已生成（存在只代表「上次登录过」，不代表现在可用）。"""
        for pattern in self.spec.keystore_globs:
            if next(self.workdir.glob(pattern), None) is not None:
                return True
        return False

    async def _confirm_session(self) -> bool:
        """用协议端的 get_login_info 确认旧会话真的可用（失败返回 False）。"""
        try:
            client = MilkyClient(
                self.conn_id,
                f"login-check-{self.conn_id}",
                f"http://127.0.0.1:{self.ctx.http_port}",
                self.ctx.access_token,
                timeout=5.0,
            )
            info = await client.get_login_info()
        except Exception as exc:  # noqa: BLE001 - 只是探测，任何异常都当作"还没好"
            log.debug("确认旧登录态失败：%s", exc)
            return False
        return bool(info)

    def _scan_codes(self, lines: list[str]) -> None:
        import re

        for line in lines[-40:]:
            match = re.search(r"[Cc]ode[=: ]+(\d+)", line)
            if not match:
                continue
            code = int(match.group(1))
            if code in (45, 237):
                self.error = diagnose_login_code(code)
                if code == 45:
                    self.state = "error"

    def snapshot(self) -> dict:
        qr = None
        if self.qr is not None:
            qr = {
                "kind": self.qr.kind,
                "data_uri": self.qr.data_uri,
                "url": self.qr.url,
                "text": self.qr.text,
                "sha1": self.qr.sha1,
            }
        return {
            "conn_id": self.conn_id,
            "state": self.state,
            "error": self.error,
            "attempts": self.attempts,
            "qr": qr,
            "qr_expires_in": max(0, QR_TTL - (now_ts() - self.qr_at)) if self.qr_at else 0,
            "done": self.state == "logged_in",
        }


class LoginManager:
    def __init__(self) -> None:
        self._flows: dict[int, LoginFlow] = {}
        self._watcher: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> dict[int, LoginFlow]:
        return self._flows

    def snapshot(self, conn_id: int) -> dict | None:
        flow = self._flows.get(conn_id)
        return flow.snapshot() if flow else None

    def log_tail(self, conn_id: int, n: int = 60) -> str:
        flow = self._flows.get(conn_id)
        if flow is None or flow.process is None:
            return ""
        return flow.process.tail_text(n)

    async def start(self, conn_id: int) -> dict:
        existing = self._flows.get(conn_id)
        if existing is not None and existing.process is not None and existing.process.running:
            return existing.snapshot()
        async with self._lock:
            async with session_scope() as session:
                conn = await session.get(BotConnection, conn_id)
                if conn is None:
                    raise ValueError("连接不存在")
                spec = get_backend(conn.backend)
                if spec is None:
                    raise ValueError(f"不支持的内置协议端：{conn.backend}")
                if conn.transport != "milky" or conn.mode != "builtin":
                    raise ValueError("只有「内置 + Milky」的连接才需要托管进程")

                sign = (
                    await session.execute(
                        select(SignProvider)
                        .where(SignProvider.bot_connection_id == conn_id)
                        .order_by(SignProvider.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                sign_url = sign.base_url if sign else ""
                sign_token = sign.token if sign else ""

                # 签名前置校验：不通过就不要拉起进程，避免用户对着 Code 45 干瞪眼
                if spec.requires_sign:
                    if not sign_url:
                        raise ValueError(
                            "这个协议端需要签名服务器，但尚未配置。三条出路：①去设置页启动自建签名；"
                            "②填一个可用的远程签名服务与 Token；③改用免签的 OneBot11 协议端（如 LLBot）。"
                        )
                    check = await check_sign(sign_url, sign_token, expected_version=conn.protocol_version or "")
                    if not check.ok:
                        raise ValueError(f"签名体检未通过：{check.message}")

                port = _free_port(conn.http_port or None)
                token = conn.token or secrets.token_urlsafe(18)
                webhook = f"http://127.0.0.1:{runtime.settings.port}/webhook/milky"
                ctx = LaunchContext(
                    uin=conn.uin,
                    http_host="127.0.0.1",
                    http_port=port,
                    access_token=token,
                    webhook_url=webhook,
                    sign_url=sign_url,
                    sign_token=sign_token,
                    protocol_os=conn.protocol_os or "Linux",
                    protocol_version=conn.protocol_version or "fetched",
                )
                workdir = runtime.settings.protocol_dir / f"{spec.key}-{conn_id}"
                flow = LoginFlow(
                    conn_id=conn_id,
                    uin=conn.uin,
                    spec=spec,
                    workdir=workdir,
                    ctx=ctx,
                    log_path=runtime.settings.logs_dir / f"{spec.key}-{conn_id}.log",
                )
                self._flows[conn_id] = flow

                # 把自动生成的连接信息写回数据库，方便 Milky 客户端接管
                conn.api_url = f"http://127.0.0.1:{port}"
                conn.token = token
                conn.workdir = str(workdir)
                conn.state = "preparing"
                session.add(
                    LoginSession(
                        bot_connection_id=conn_id,
                        state="preparing",
                        started_at=now_ts(),
                    )
                )

            try:
                await flow.launch()
            except Exception as exc:
                flow.state = "error"
                flow.error = str(exc)
                await self._persist(flow)
                raise
            self._ensure_watcher()
            return flow.snapshot()

    async def restart(self, conn_id: int) -> dict:
        flow = self._flows.get(conn_id)
        if flow is None:
            return await self.start(conn_id)
        flow.attempts = 0
        flow.error = ""
        if flow.process is not None:
            await flow.process.stop()
        await flow.launch()
        return flow.snapshot()

    async def stop(self, conn_id: int) -> None:
        flow = self._flows.pop(conn_id, None)
        if flow is not None:
            await flow.shutdown()
            await self._persist(flow, final=True)

    async def stop_all(self) -> None:
        for conn_id in list(self._flows):
            await self.stop(conn_id)
        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):
                pass
            self._watcher = None

    def _ensure_watcher(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch(), name="livepush-login-watcher")

    async def _watch(self) -> None:
        while True:
            for conn_id, flow in list(self._flows.items()):
                try:
                    await flow.poll()
                    await self._persist(flow)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("登录轮询异常 conn=%s", conn_id)
            await asyncio.sleep(POLL_INTERVAL)

    async def _persist(self, flow: LoginFlow, final: bool = False) -> None:
        try:
            async with session_scope() as session:
                conn = await session.get(BotConnection, flow.conn_id)
                if conn is not None:
                    conn.state = flow.state
                    conn.last_error = flow.error
                    conn.updated_at = now_ts()
                last = (
                    await session.execute(
                        LoginSession.__table__.select()
                        .where(LoginSession.bot_connection_id == flow.conn_id)
                        .order_by(LoginSession.id.desc())
                        .limit(1)
                    )
                ).first()
                if last is not None:
                    await session.execute(
                        LoginSession.__table__.update()
                        .where(LoginSession.id == last.id)
                        .values(
                            state=flow.state,
                            qr_sha1=flow.qr.sha1 if flow.qr else "",
                            attempts=flow.attempts,
                            error=flow.error[:500],
                            ended_at=now_ts() if final or flow.state == "logged_in" else 0,
                        )
                    )
        except Exception:
            log.exception("持久化登录状态失败")

        if flow.state == "logged_in":
            # 让 Milky 客户端刷新一次登录信息
            client = registry.manager.peek(flow.conn_id)
            if client is not None and hasattr(client, "refresh"):
                try:
                    await client.refresh()  # type: ignore[attr-defined]
                except Exception:
                    pass


login_manager = LoginManager()
