"""HTML 页面路由。"""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import is_authenticated
from .deps import templates

router = APIRouter()


def _guard(request: Request) -> RedirectResponse | None:
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return None


@router.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"active": "login"})


@router.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    if redirect := _guard(request):
        return redirect
    return templates.TemplateResponse(request, "dashboard.html", {"active": "dashboard"})


@router.get("/rooms", response_class=HTMLResponse)
async def page_rooms(request: Request):
    if redirect := _guard(request):
        return redirect
    return templates.TemplateResponse(request, "rooms.html", {"active": "rooms"})


@router.get("/targets", response_class=HTMLResponse)
async def page_targets(request: Request):
    if redirect := _guard(request):
        return redirect
    return templates.TemplateResponse(request, "targets.html", {"active": "targets"})


@router.get("/bindings", response_class=HTMLResponse)
async def page_bindings(request: Request):
    if redirect := _guard(request):
        return redirect
    return templates.TemplateResponse(request, "bindings.html", {"active": "bindings"})


@router.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request):
    if redirect := _guard(request):
        return redirect
    return templates.TemplateResponse(request, "logs.html", {"active": "logs"})


@router.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    if redirect := _guard(request):
        return redirect
    return templates.TemplateResponse(request, "settings.html", {"active": "settings"})


@router.get("/qq", response_class=HTMLResponse)
async def page_qq(request: Request):
    if redirect := _guard(request):
        return redirect
    return templates.TemplateResponse(request, "qq.html", {"active": "qq"})


@router.get("/hosted/{bot_id}", response_class=HTMLResponse)
async def page_hosted_ui(request: Request, bot_id: int):
    """内嵌托管协议端的 WebUI（同源反向代理，见 web/proxy.py）。"""
    if redirect := _guard(request):
        return redirect
    from ..qq.hosted import hosted_manager

    webui: dict = {"token": "", "source": "", "port": 0}
    public_url = ""
    with contextlib.suppress(Exception):
        webui = await hosted_manager.webui_token(bot_id)
    with contextlib.suppress(Exception):
        public_url = await hosted_manager.public_webui_url(bot_id)
    return templates.TemplateResponse(
        request,
        "hosted_ui.html",
        # active 必须是独立的页面名：传 "qq" 会让 app.js 在托管页跑 QQ 页逻辑，
        # #b-mode 不存在 → addEventListener of null，就是「打不开 WebUI」的报错来源。
        {"active": "hosted", "bot_id": bot_id, "webui": webui, "public_url": public_url},
    )
