"""FastAPI 应用装配。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import state
from .config import Runtime, runtime
from .db import dispose_engine, init_engine, init_schema, session_scope
from .models import BotConnection, SignProvider
from .monitor.engine import MonitorEngine
from .monitor.events import bus
from .push.dispatcher import Dispatcher
from .qq import registry
from .qq.backends import get_backend
from .qq.hosted import hosted_manager
from .qq.login import login_manager
from .qq.sign_server import sign_server
from .util.logging import setup_logging
from .util.logretention import setup_retention, truncate_service_log
from .alerts import alert_manager, configure_alerts, install_alerts
from .qq.watchdog import watchdog
from .web import routes_api, routes_pages, sse
from .web import proxy as web_proxy
from .web.deps import STATIC_DIR

log = logging.getLogger("livepush")


async def _connect_external_bots() -> None:
    async with session_scope() as session:
        bots = (
            await session.execute(
                select(BotConnection).where(BotConnection.enabled.is_(True), BotConnection.mode == "external")
            )
        ).scalars().all()
    for bot in bots:
        try:
            await registry.manager.get(bot)
            log.info("外接机器人已连接：%s", bot.name)
        except Exception as exc:
            log.warning("外接机器人连接失败 %s：%s", bot.name, exc)


async def _resume_builtin_bots() -> None:
    """重启后：若已有登录态文件，直接拉起协议端免扫码上线。"""
    async with session_scope() as session:
        bots = (
            await session.execute(
                select(BotConnection).where(BotConnection.enabled.is_(True), BotConnection.mode == "builtin")
            )
        ).scalars().all()
    for bot in bots:
        spec = get_backend(bot.backend)
        if spec is None:
            continue
        workdir = Path(bot.workdir) if bot.workdir else runtime.settings.protocol_dir / f"{spec.key}-{bot.id}"
        has_keystore = any(any(workdir.glob(pattern)) for pattern in spec.keystore_globs)
        if not has_keystore:
            continue
        try:
            await login_manager.start(bot.id)
            log.info("已用登录态拉起协议端：%s", bot.name)
        except Exception as exc:
            log.warning("恢复 %s 失败：%s", bot.name, exc)


async def _autostart_sign() -> None:
    async with session_scope() as session:
        providers = (
            await session.execute(select(SignProvider).where(SignProvider.kind == "selfhosted"))
        ).scalars().all()
    if not providers:
        return
    if not sign_server.installed:
        log.warning("配置了自建签名，但 %s 下缺少 sign.py / wrapper.node", sign_server.dir)
        return
    result = await sign_server.start()
    log.info("自建签名：%s", result.get("message"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Runtime.load()
    settings = runtime.settings
    setup_logging(settings.log_level, settings.logs_dir / "app.log")
    if runtime.password_generated:
        log.warning("已生成管理员口令并写入 %s：%s", settings.abs_data_dir / "admin-password.txt", runtime.admin_password)

    await init_engine(settings.db_url)
    await init_schema()

    from .appconfig import load_app_config

    async with session_scope() as session:
        boot_config = await load_app_config(session)
    truncate_service_log(settings.logs_dir / "service.log", boot_config.log_service_truncate_mb)
    retention = setup_retention(settings.logs_dir)
    await retention.load_state()
    await retention.start()

    configure_alerts(boot_config)
    install_alerts(alert_manager)
    alert_manager.start()

    engine = MonitorEngine(settings.covers_dir)
    dispatcher = Dispatcher(settings.covers_dir)
    state.set_services(engine, dispatcher)
    await dispatcher.start()
    await engine.start()

    async def bridge_wakeups() -> None:
        """监控引擎产生新任务时立刻叫醒派发器，避免等下一次轮询。"""
        while True:
            await engine.wakeup.wait()
            engine.wakeup.clear()
            dispatcher.wake()

    bridge_task = asyncio.create_task(bridge_wakeups(), name="livepush-wakeup-bridge")

    await _autostart_sign()
    await _connect_external_bots()
    await _resume_builtin_bots()
    await hosted_manager.resume_all()
    watchdog.start()
    log.info("livepush 已就绪：http://%s:%s", settings.host, settings.port)

    try:
        yield
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except (asyncio.CancelledError, Exception):
            pass
        await retention.stop()
        await alert_manager.stop()
        await watchdog.stop()
        await web_proxy.close_client()
        await login_manager.stop_all()
        await hosted_manager.stop_all()
        await registry.manager.stop_all()
        await sign_server.stop()
        await engine.stop()
        await dispatcher.stop()
        await dispose_engine()
        log.info("livepush 已停止")


def create_app() -> FastAPI:
    app = FastAPI(title="livepush", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(routes_api.public)
    app.include_router(routes_api.router)
    app.include_router(sse.router)
    app.include_router(routes_pages.router)
    app.include_router(web_proxy.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/favicon.ico")
    async def favicon():
        return RedirectResponse("/static/favicon.svg", status_code=307)

    @app.post("/webhook/milky")
    async def milky_webhook(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        event = payload.get("event") if isinstance(payload, dict) else None
        if event:
            bus.publish({"type": "milky_event", "event": event})
        return JSONResponse({"status": "ok", "retcode": 0, "data": None})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("未处理异常 %s", request.url.path)
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    return app


app = create_app()
