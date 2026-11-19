"""HTML 页面路由。"""

from __future__ import annotations

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
