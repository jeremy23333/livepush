"""JSON API。

约定：
- 所有非 GET 请求必须带 `X-LP: 1`（防 CSRF，见 auth.require_csrf）；
- 返回值统一是 `{"ok": true, ...}` 或 FastAPI 的错误结构。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import ssl
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..appconfig import AppConfig, load_app_config, save_app_config
from ..bili.client import BiliError, BiliNotFound, BiliRiskControl
from ..config import runtime
from ..db import session_scope
from ..models import (
    BotConnection,
    LiveEvent,
    LiveRoom,
    PushJob,
    PushTarget,
    SignProvider,
    Subscription,
    now_ts,
)
from ..qq import registry
from ..qq.backends import BACKENDS
from ..qq.hosted import BACKENDS as HOSTED_BACKENDS
from ..qq.hosted import HostedError, hosted_manager
from ..qq.login import login_manager
from ..qq.signer import check_sign, normalize_base
from ..state import get_dispatcher, get_engine
from ..util.timefmt import fmt_dt, fmt_duration
from .auth import COOKIE_NAME, SESSION_TTL, issue_session, require_csrf, require_user, verify_password
from .deps import EVENT_LABELS, JOB_STATUS_LABELS, ROOM_STATE_LABELS, STATE_LABELS

log = logging.getLogger("livepush.api")

public = APIRouter(prefix="/api", tags=["public"])
router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_user), Depends(require_csrf)])


# --------------------------------------------------------------------------- 序列化
def _room(room: LiveRoom, bind_count: int = 0) -> dict:
    return {
        "id": room.id,
        "uid": room.uid,
        "room_id": room.room_id,
        "short_id": room.short_id,
        "uname": room.uname,
        "face": room.face,
        "title": room.title,
        "area": f"{room.area_parent} / {room.area_name}".strip(" /") or "未知分区",
        "cover_url": room.cover_url,
        "live_status": room.live_status,
        "live": room.live_status == 1,
        "live_started_at": room.live_started_at,
        "live_started_text": fmt_dt(room.live_started_at),
        "room_state": room.room_state,
        "room_state_label": ROOM_STATE_LABELS.get(room.room_state, room.room_state),
        "fail_streak": room.fail_streak,
        "last_checked_text": fmt_dt(room.last_checked_at),
        "enabled": room.enabled,
        "notify_on_add": room.notify_on_add,
        "note": room.note,
        "url": room.url,
        "bindings": bind_count,
    }


def _target(target: PushTarget, bot: BotConnection | None = None) -> dict:
    return {
        "id": target.id,
        "bot_connection_id": target.bot_connection_id,
        "bot_name": (bot.name if bot else "") or f"bot-{target.bot_connection_id}",
        "kind": target.kind,
        "target_id": target.target_id,
        "name": target.name,
        "enabled": target.enabled,
    }


def _bot(conn: BotConnection, sign: SignProvider | None = None) -> dict:
    return {
        "id": conn.id,
        "name": conn.name,
        "transport": conn.transport,
        "mode": conn.mode,
        "backend": conn.backend,
        "backend_label": BACKENDS[conn.backend].label if conn.backend in BACKENDS else conn.backend,
        "api_url": conn.api_url,
        "ws_url": conn.ws_url,
        "token": conn.token,
        "uin": conn.uin,
        "protocol_os": conn.protocol_os,
        "protocol_version": conn.protocol_version,
        "state": conn.state,
        "state_label": STATE_LABELS.get(conn.state, conn.state),
        "last_error": conn.last_error,
        "self_id": conn.self_id,
        "nickname": conn.nickname,
        "enabled": conn.enabled,
        "sign": {
            "kind": sign.kind if sign else "none",
            "base_url": sign.base_url if sign else "",
            "token": sign.token if sign else "",
            "health": sign.health if sign else "unknown",
            "protocol_version": sign.protocol_version if sign else "fetched",
        },
    }


# --------------------------------------------------------------------------- 登录
class LoginBody(BaseModel):
    password: str = ""


@public.post("/login")
async def api_login(body: LoginBody, response: Response) -> dict:
    if not verify_password(body.password):
        raise HTTPException(401, "口令不正确")
    response.set_cookie(
        COOKIE_NAME,
        issue_session(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@public.post("/logout")
async def api_logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# --------------------------------------------------------------------------- 总览
@router.get("/overview")
async def api_overview() -> dict:
    engine = get_engine()
    dispatcher = get_dispatcher()
    async with session_scope() as session:
        room_total = (await session.execute(select(func.count(LiveRoom.id)))).scalar_one()
        live_total = (
            await session.execute(select(func.count(LiveRoom.id)).where(LiveRoom.live_status == 1))
        ).scalar_one()
        target_total = (await session.execute(select(func.count(PushTarget.id)))).scalar_one()
        bot_total = (await session.execute(select(func.count(BotConnection.id)))).scalar_one()
        today_start = now_ts() - (now_ts() % 86400)
        sent_today = (
            await session.execute(
                select(func.count(PushJob.id)).where(PushJob.status == "sent", PushJob.updated_at >= today_start)
            )
        ).scalar_one()
        failed_today = (
            await session.execute(
                select(func.count(PushJob.id)).where(PushJob.status == "failed", PushJob.updated_at >= today_start)
            )
        ).scalar_one()
        pending = (
            await session.execute(select(func.count(PushJob.id)).where(PushJob.status == "pending"))
        ).scalar_one()
        events = (
            await session.execute(
                select(LiveEvent, LiveRoom)
                .join(LiveRoom, LiveEvent.room_id == LiveRoom.id)
                .order_by(LiveEvent.id.desc())
                .limit(20)
            )
        ).all()
        bots = (await session.execute(select(BotConnection))).scalars().all()
        targets_need_bot = len(bots) == 0

    return {
        "ok": True,
        "engine": engine.status(),
        "dispatcher": {
            "running": bool(dispatcher._task and not dispatcher._task.done()),
            "sent": dispatcher.sent_count,
            "failed": dispatcher.failed_count,
            "last_error": dispatcher.last_error,
        },
        "counts": {
            "rooms": room_total,
            "live": live_total,
            "targets": target_total,
            "bots": bot_total,
            "sent_today": sent_today,
            "failed_today": failed_today,
            "pending": pending,
        },
        "bots": [
            {
                "id": b.id,
                "name": b.name,
                "transport": b.transport,
                "backend": b.backend,
                "state": b.state,
                "state_label": STATE_LABELS.get(b.state, b.state),
                "nickname": b.nickname,
                "self_id": b.self_id,
            }
            for b in bots
        ],
        "need_bot": targets_need_bot,
        "events": [
            {
                "id": event.id,
                "kind": event.kind,
                "label": EVENT_LABELS.get(event.kind, event.kind),
                "uname": room.uname,
                "title": room.title,
                "at_text": fmt_dt(event.created_at),
                "duration": fmt_duration(event.duration) if event.duration else "",
            }
            for event, room in events
        ],
        "clients": registry.manager.snapshot(),
    }


# --------------------------------------------------------------------------- 直播间
class ResolveBody(BaseModel):
    query: str


class RoomCreateBody(BaseModel):
    query: str
    notify_on_add: bool = False
    note: str = ""
    # 预览时解析出的 UID：用于发现「确认时二次解析结果与预览不一致」的情况
    expected_uid: int = 0


class RoomPatchBody(BaseModel):
    enabled: bool | None = None
    notify_on_add: bool | None = None
    note: str | None = None


@router.get("/rooms")
async def api_rooms() -> dict:
    async with session_scope() as session:
        rooms = (await session.execute(select(LiveRoom).order_by(LiveRoom.id))).scalars().all()
        counts = dict(
            (
                await session.execute(
                    select(Subscription.room_id, func.count(Subscription.id)).group_by(Subscription.room_id)
                )
            ).all()
        )
    return {"ok": True, "rooms": [_room(r, counts.get(r.id, 0)) for r in rooms]}


def _bili_detail(exc: BiliError, elapsed: float = 0.0) -> str:
    """把分阶段错误翻成用户能看懂的一句话（前端直接展示 detail）。"""
    names = {
        "room_id": "按房间号查询",
        "room_init": "查询房间号（旧接口）",
        "room_info": "取房间详情",
        "status": "批量查询直播状态",
        "master_info": "取主播信息",
        "uid": "按 UID 查询",
        "resolve": "解析输入",
        "short_link": "展开短链",
    }
    where = names.get(exc.stage or "", exc.stage or "")
    suffix = ""
    if exc.code is not None:
        suffix = f"（B 站 code={exc.code}）"
    if elapsed >= 0.1:
        suffix += f" · 用时 {elapsed:.1f}s"
    return f"{where}失败：{exc}{suffix}" if where else f"{exc}{suffix}"


def _log_bili_error(action: str, query: str, exc: BiliError) -> None:
    expected = isinstance(exc, BiliNotFound)
    log.warning(
        "%s失败 query=%r stage=%s code=%s: %s",
        action,
        query,
        exc.stage or "-",
        exc.code,
        exc,
        exc_info=not expected,
    )


@router.post("/rooms/resolve")
async def api_rooms_resolve(body: ResolveBody) -> dict:
    engine = get_engine()
    client = engine.bili
    if client is None:
        raise HTTPException(503, "采集客户端尚未初始化")
    started = time.perf_counter()
    try:
        resolved = await client.resolve(body.query)
    except BiliRiskControl as exc:
        _log_bili_error("解析直播间", body.query, exc)
        raise HTTPException(429, _bili_detail(exc, time.perf_counter() - started)) from exc
    except BiliNotFound as exc:
        _log_bili_error("解析直播间", body.query, exc)
        raise HTTPException(404, _bili_detail(exc, time.perf_counter() - started)) from exc
    except BiliError as exc:
        _log_bili_error("解析直播间", body.query, exc)
        raise HTTPException(502, _bili_detail(exc, time.perf_counter() - started)) from exc
    return {
        "ok": True,
        "room": {
            "uid": resolved.uid,
            "room_id": resolved.room_id,
            "short_id": resolved.short_id,
            "uname": resolved.uname,
            "face": resolved.face,
            "title": resolved.title,
            "area": f"{resolved.area_parent} / {resolved.area_name}".strip(" /"),
            "live_status": resolved.live_status,
            "live": resolved.live_status == 1,
            "url": resolved.url,
            "input_kind": resolved.input_kind,
            "input_number": resolved.input_number,
            "resolved_as": resolved.resolved_as,
            "ambiguous": resolved.ambiguous,
        },
    }


@router.post("/rooms")
async def api_rooms_create(body: RoomCreateBody) -> dict:
    engine = get_engine()
    client = engine.bili
    if client is None:
        raise HTTPException(503, "采集客户端尚未初始化")
    started = time.perf_counter()
    try:
        resolved = await client.resolve(body.query)
    except BiliRiskControl as exc:
        _log_bili_error("添加直播间", body.query, exc)
        raise HTTPException(429, _bili_detail(exc, time.perf_counter() - started)) from exc
    except BiliNotFound as exc:
        _log_bili_error("添加直播间", body.query, exc)
        raise HTTPException(404, _bili_detail(exc, time.perf_counter() - started)) from exc
    except BiliError as exc:
        _log_bili_error("添加直播间", body.query, exc)
        raise HTTPException(502, _bili_detail(exc, time.perf_counter() - started)) from exc

    if body.expected_uid and resolved.uid != body.expected_uid:
        raise HTTPException(
            409,
            f"解析结果与预览不一致（预览 UID {body.expected_uid}，现在 UID {resolved.uid}）；"
            "请重新解析确认，避免把别的直播间加进来",
        )
    if not resolved.room_id:
        # 二次解析没拿到房间号时再兜一次；仍然没有就允许落库，但 URL 会退回空间链接。
        with contextlib.suppress(BiliError):
            room_id, short_id = await client.room_id_by_uid(resolved.uid)
            resolved.room_id = resolved.room_id or room_id
            resolved.short_id = resolved.short_id or short_id
        if not resolved.room_id:
            log.warning("添加直播间 %s 没取到房间号，链接将退回空间页", resolved.uid)

    async with session_scope() as session:
        exists = (
            await session.execute(select(LiveRoom).where(LiveRoom.uid == resolved.uid))
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(409, f"该直播间已存在（{exists.uname or exists.uid}）")
        room = LiveRoom(
            uid=resolved.uid,
            room_id=resolved.room_id,
            short_id=resolved.short_id,
            uname=resolved.uname,
            face=resolved.face,
            title=resolved.title,
            area_name=resolved.area_name,
            area_parent=resolved.area_parent,
            cover_url=resolved.cover_url,
            live_status=resolved.live_status,
            room_state="new",
            notify_on_add=body.notify_on_add,
            note=body.note,
        )
        session.add(room)
        await session.flush()
        payload = _room(room)
    engine.trigger_now()
    return {"ok": True, "room": payload}


@router.get("/logs")
async def api_logs(level: str = "", limit: int = 300) -> dict:
    """最近 N 条内存日志（实时面板首屏）。"""
    from ..util.logbuf import AREA_NAMES, get_buffer

    buf = get_buffer()
    return {
        "ok": True,
        "lines": buf.snapshot(level, limit),
        "seq": buf.seq,
        "areas": AREA_NAMES,
    }


@router.delete("/logs")
async def api_logs_clear() -> dict:
    from ..util.logbuf import get_buffer

    get_buffer().clear()
    return {"ok": True}


@router.get("/logs/stream")
async def api_logs_stream(request: Request, level: str = "", cursor: int = 0) -> StreamingResponse:
    """SSE 增量日志；`cursor` 是上次拿到的最大 seq。"""
    from ..util.logbuf import get_buffer

    async def generator():
        yield "retry: 3000\n\n"
        buf = get_buffer()
        last = max(0, cursor)
        while True:
            if await request.is_disconnected():
                break
            items = buf.since(last, level)
            if items:
                last = items[-1]["seq"]
                payload = {"type": "log", "lines": items}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/logs/files")
async def api_log_files() -> dict:
    """日志目录清单（供设置页展示 + 手动清理）。"""
    from ..util.logretention import list_log_files

    logs_dir = runtime.settings.logs_dir
    return {"ok": True, "dir": str(logs_dir), "files": list_log_files(logs_dir)}


@router.post("/logs/cleanup")
async def api_logs_cleanup(dry_run: bool = False) -> dict:
    from ..util.logretention import cleanup_logs

    async with session_scope() as session:
        config = await load_app_config(session)
    if config.log_retention_days <= 0:
        raise HTTPException(400, "自动清理已关闭（日志保留天数为 0），先在设置里改成正数")
    report = cleanup_logs(
        config.log_retention_days,
        logs_dir=runtime.settings.logs_dir,
        dry_run=dry_run,
    )
    log.info(
        "手动清理日志：删除 %d 个，释放 %.1f KB%s",
        len(report["deleted"]),
        report["freed_bytes"] / 1024,
        "（预演）" if dry_run else "",
    )
    return {"ok": True, "report": report}


@router.get("/diag/bili")
async def api_diag_bili(query: str) -> dict:
    """逐项体检「添加直播间」链路（输入定型 → 房间接口 → UID 对照 → 批量状态）。"""
    client = get_engine().bili
    if client is None:
        raise HTTPException(503, "采集客户端尚未初始化")
    try:
        return await client.diagnose(query)
    except BiliError as exc:
        return {"ok": False, "steps": [], "summary": _bili_detail(exc)}


@router.get("/diag/network")
async def api_diag_network() -> dict:
    """只体检「服务器到 B 站」的网络链路，用来把 502 分成网络问题还是应用问题。"""
    host = "api.live.bilibili.com"
    steps: list[dict] = []

    def add(name: str, ok: bool, ms: float = 0.0, detail: str = "") -> None:
        steps.append({"name": name, "ok": ok, "ms": int(ms), "detail": detail})

    start = time.perf_counter()
    ips: list[str] = []
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM)
        ips = sorted({item[4][0] for item in infos})
        add("DNS", True, (time.perf_counter() - start) * 1000, "、".join(ips[:4]) or f"{len(infos)} 条记录")
    except OSError as exc:
        add("DNS", False, (time.perf_counter() - start) * 1000, str(exc))
        return {
            "ok": False,
            "steps": steps,
            "summary": "DNS 解析失败：检查 /etc/resolv.conf、云厂商 DNS 或容器内 DNS。",
        }

    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 443), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        add("TCP 443", True, (time.perf_counter() - start) * 1000, "握手成功")
    except (OSError, asyncio.TimeoutError) as exc:
        add("TCP 443", False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}")
        return {
            "ok": False,
            "steps": steps,
            "summary": "TCP 连不上 443：服务器出网被拦（安全组 / 防火墙 / 出口代理）。",
        }

    start = time.perf_counter()
    tls_version = ""
    try:
        context = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=context, server_hostname=host), timeout=8.0
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        tls_version = getattr(ssl_obj, "version", lambda: "")() or ""
        writer.close()
        await writer.wait_closed()
        add("TLS", True, (time.perf_counter() - start) * 1000, tls_version or "握手成功")
    except (OSError, asyncio.TimeoutError) as exc:
        add("TLS", False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}")
        return {
            "ok": False,
            "steps": steps,
            "summary": "TLS 握手失败：检查系统时间、CA 证书（ca-certificates）或中间人代理。",
        }

    async with session_scope() as session:
        cfg = await load_app_config(session)
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            trust_env=False,
            proxy=(cfg.bili_proxy or None),
        ) as cl:
            resp = await cl.get(f"https://{host}/room/v1/Room/get_info", params={"room_id": 6})
        ok = resp.status_code < 500
        add(
            "HTTP 接口",
            ok,
            (time.perf_counter() - start) * 1000,
            f"HTTP {resp.status_code}，{len(resp.content)} 字节",
        )
    except httpx.HTTPError as exc:
        add("HTTP 接口", False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}")
        return {
            "ok": False,
            "steps": steps,
            "summary": "网络能通，但 HTTP 请求失败：多半是代理配置（bili_proxy）或 B 站临时限制。",
        }

    ok = all(step["ok"] for step in steps)
    if ok:
        proxy_hint = f"（走代理 {cfg.bili_proxy}）" if cfg.bili_proxy else "（直连，未使用代理）"
        summary = f"服务器到 B 站网络正常{proxy_hint}。若这时添加直播间仍失败，就是接口/输入的问题，用『诊断输入』查。"
    else:
        summary = "有步骤失败，按第一个 FAIL 项排查。"
    return {"ok": ok, "steps": steps, "summary": summary}


@router.patch("/rooms/{room_id}")
async def api_rooms_patch(room_id: int, body: RoomPatchBody) -> dict:
    async with session_scope() as session:
        room = await session.get(LiveRoom, room_id)
        if room is None:
            raise HTTPException(404, "直播间不存在")
        if body.enabled is not None:
            room.enabled = body.enabled
        if body.notify_on_add is not None:
            room.notify_on_add = body.notify_on_add
        if body.note is not None:
            room.note = body.note
        room.updated_at = now_ts()
        return {"ok": True, "room": _room(room)}


@router.delete("/rooms/{room_id}")
async def api_rooms_delete(room_id: int) -> dict:
    async with session_scope() as session:
        room = await session.get(LiveRoom, room_id)
        if room is None:
            raise HTTPException(404, "直播间不存在")
        await session.delete(room)
    return {"ok": True}


@router.post("/rooms/{room_id}/check")
async def api_rooms_check(room_id: int) -> dict:
    engine = get_engine()
    engine.trigger_now()
    return {"ok": True, "message": "已安排下一轮检查"}


@router.get("/rooms/{room_id}/events")
async def api_room_events(room_id: int, limit: int = 30) -> dict:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(LiveEvent).where(LiveEvent.room_id == room_id).order_by(LiveEvent.id.desc()).limit(limit)
            )
        ).scalars().all()
    return {
        "ok": True,
        "events": [
            {
                "id": e.id,
                "kind": e.kind,
                "label": EVENT_LABELS.get(e.kind, e.kind),
                "at_text": fmt_dt(e.created_at),
                "duration": fmt_duration(e.duration) if e.duration else "",
                "snapshot": json.loads(e.snapshot or "{}"),
            }
            for e in rows
        ],
    }


# --------------------------------------------------------------------------- 推送目标
class TargetCreateBody(BaseModel):
    bot_connection_id: int
    kind: str = "group"
    target_id: str
    name: str = ""


class TargetPatchBody(BaseModel):
    enabled: bool | None = None
    name: str | None = None


@router.get("/targets")
async def api_targets() -> dict:
    async with session_scope() as session:
        targets = (await session.execute(select(PushTarget).order_by(PushTarget.id))).scalars().all()
        bots = {b.id: b for b in (await session.execute(select(BotConnection))).scalars().all()}
    return {"ok": True, "targets": [_target(t, bots.get(t.bot_connection_id)) for t in targets]}


@router.post("/targets")
async def api_targets_create(body: TargetCreateBody) -> dict:
    if body.kind not in ("group", "private"):
        raise HTTPException(400, "kind 只能是 group 或 private")
    if not body.target_id.strip().lstrip("-").isdigit():
        raise HTTPException(400, "群号 / QQ 号必须是数字")
    async with session_scope() as session:
        bot = await session.get(BotConnection, body.bot_connection_id)
        if bot is None:
            raise HTTPException(404, "机器人连接不存在")
        exists = (
            await session.execute(
                select(PushTarget).where(
                    PushTarget.bot_connection_id == body.bot_connection_id,
                    PushTarget.kind == body.kind,
                    PushTarget.target_id == body.target_id.strip(),
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(409, "该推送目标已存在")
        target = PushTarget(
            bot_connection_id=body.bot_connection_id,
            kind=body.kind,
            target_id=body.target_id.strip(),
            name=body.name.strip() or body.target_id.strip(),
        )
        session.add(target)
        await session.flush()
        payload = _target(target, bot)
    return {"ok": True, "target": payload}


@router.patch("/targets/{target_id}")
async def api_targets_patch(target_id: int, body: TargetPatchBody) -> dict:
    async with session_scope() as session:
        target = await session.get(PushTarget, target_id)
        if target is None:
            raise HTTPException(404, "推送目标不存在")
        if body.enabled is not None:
            target.enabled = body.enabled
        if body.name is not None:
            target.name = body.name
        target.updated_at = now_ts()
        return {"ok": True, "target": _target(target)}


@router.delete("/targets/{target_id}")
async def api_targets_delete(target_id: int) -> dict:
    async with session_scope() as session:
        target = await session.get(PushTarget, target_id)
        if target is None:
            raise HTTPException(404, "推送目标不存在")
        await session.delete(target)
    return {"ok": True}


class ImportGroupsBody(BaseModel):
    bot_connection_id: int


@router.post("/targets/import-groups")
async def api_targets_import(body: ImportGroupsBody) -> dict:
    async with session_scope() as session:
        bot = await session.get(BotConnection, body.bot_connection_id)
        if bot is None:
            raise HTTPException(404, "机器人连接不存在")
        client = await registry.manager.get(bot)
        groups = await client.get_group_list()
        created = 0
        for group in groups:
            exists = (
                await session.execute(
                    select(PushTarget).where(
                        PushTarget.bot_connection_id == bot.id,
                        PushTarget.kind == "group",
                        PushTarget.target_id == group["id"],
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    PushTarget(
                        bot_connection_id=bot.id,
                        kind="group",
                        target_id=group["id"],
                        name=group.get("name") or group["id"],
                    )
                )
                created += 1
        await session.flush()
    message = f"导入 {created} 个群（共拉取 {len(groups)} 个）"
    if not groups:
        message = "拉取到 0 个群：机器人可能未连接，或协议端没返回群列表。先在「QQ 账号与登录」确认状态。"
    return {"ok": True, "imported": created, "total": len(groups), "message": message}


# --------------------------------------------------------------------------- 绑定关系
class BindingBody(BaseModel):
    room_id: int
    target_id: int
    notify_live_on: bool = True
    notify_live_off: bool = True
    notify_title_change: bool = False
    at_all: bool = False
    include_cover: bool = True
    message_template: str = ""
    enabled: bool = True


@router.get("/bindings")
async def api_bindings() -> dict:
    async with session_scope() as session:
        rooms = (await session.execute(select(LiveRoom).order_by(LiveRoom.id))).scalars().all()
        targets = (await session.execute(select(PushTarget).order_by(PushTarget.id))).scalars().all()
        subs = (await session.execute(select(Subscription))).scalars().all()
    return {
        "ok": True,
        "rooms": [{"id": r.id, "uname": r.uname, "face": r.face, "live": r.live_status == 1} for r in rooms],
        "targets": [
            {"id": t.id, "kind": t.kind, "name": t.name, "target_id": t.target_id, "bot_connection_id": t.bot_connection_id}
            for t in targets
        ],
        "bindings": [
            {
                "id": s.id,
                "room_id": s.room_id,
                "target_id": s.target_id,
                "notify_live_on": s.notify_live_on,
                "notify_live_off": s.notify_live_off,
                "notify_title_change": s.notify_title_change,
                "at_all": s.at_all,
                "include_cover": s.include_cover,
                "message_template": s.message_template,
                "enabled": s.enabled,
            }
            for s in subs
        ],
    }


@router.post("/bindings")
async def api_bindings_upsert(body: BindingBody) -> dict:
    async with session_scope() as session:
        room = await session.get(LiveRoom, body.room_id)
        target = await session.get(PushTarget, body.target_id)
        if room is None or target is None:
            raise HTTPException(404, "直播间或推送目标不存在")
        sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.room_id == body.room_id, Subscription.target_id == body.target_id
                )
            )
        ).scalar_one_or_none()
        if sub is None:
            sub = Subscription(room_id=body.room_id, target_id=body.target_id)
            session.add(sub)
        sub.notify_live_on = body.notify_live_on
        sub.notify_live_off = body.notify_live_off
        sub.notify_title_change = body.notify_title_change
        sub.at_all = body.at_all
        sub.include_cover = body.include_cover
        sub.message_template = body.message_template
        sub.enabled = body.enabled
        await session.flush()
        return {"ok": True, "id": sub.id}


@router.delete("/bindings/{binding_id}")
async def api_bindings_delete(binding_id: int) -> dict:
    async with session_scope() as session:
        sub = await session.get(Subscription, binding_id)
        if sub is None:
            raise HTTPException(404, "绑定不存在")
        await session.delete(sub)
    return {"ok": True}


# --------------------------------------------------------------------------- 事件与推送日志
@router.get("/events")
async def api_events(limit: int = 100, kind: str = "") -> dict:
    async with session_scope() as session:
        stmt = (
            select(LiveEvent, LiveRoom)
            .join(LiveRoom, LiveEvent.room_id == LiveRoom.id)
            .order_by(LiveEvent.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        if kind:
            stmt = stmt.where(LiveEvent.kind == kind)
        rows = (await session.execute(stmt)).all()
    return {
        "ok": True,
        "events": [
            {
                "id": e.id,
                "kind": e.kind,
                "label": EVENT_LABELS.get(e.kind, e.kind),
                "uname": room.uname,
                "title": room.title,
                "at_text": fmt_dt(e.created_at),
                "duration": fmt_duration(e.duration) if e.duration else "",
            }
            for e, room in rows
        ],
    }


@router.get("/jobs")
async def api_jobs(limit: int = 100, status: str = "") -> dict:
    async with session_scope() as session:
        stmt = (
            select(PushJob, PushTarget, LiveEvent, LiveRoom)
            .join(PushTarget, PushJob.target_id == PushTarget.id)
            .join(LiveEvent, PushJob.event_id == LiveEvent.id)
            .join(LiveRoom, LiveEvent.room_id == LiveRoom.id)
            .order_by(PushJob.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        if status:
            stmt = stmt.where(PushJob.status == status)
        rows = (await session.execute(stmt)).all()
    return {
        "ok": True,
        "jobs": [
            {
                "id": job.id,
                "status": job.status,
                "status_label": JOB_STATUS_LABELS.get(job.status, job.status),
                "attempts": job.attempts,
                "error": job.last_error,
                "preview": job.preview,
                "target": f"{target.name}（{target.kind}:{target.target_id}）",
                "event": EVENT_LABELS.get(event.kind, event.kind),
                "uname": room.uname,
                "at_text": fmt_dt(job.updated_at),
            }
            for job, target, event, room in rows
        ],
    }


@router.post("/jobs/{job_id}/retry")
async def api_jobs_retry(job_id: int) -> dict:
    async with session_scope() as session:
        job = await session.get(PushJob, job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        job.status = "pending"
        job.attempts = 0
        job.next_attempt_at = now_ts()
        job.last_error = ""
        job.updated_at = now_ts()
    get_dispatcher().wake()
    return {"ok": True}


# --------------------------------------------------------------------------- 设置
@router.get("/settings")
async def api_settings_get() -> dict:
    async with session_scope() as session:
        config = await load_app_config(session)
    return {"ok": True, "config": config.to_dict()}


@router.get("/alerts/state")
async def api_alerts_state() -> dict:
    from ..alerts import alert_manager

    return {"ok": True, "state": alert_manager.status()}


@router.post("/alerts/test")
async def api_alerts_test() -> dict:
    from ..alerts import alert_manager

    result = await alert_manager.send_test()
    return {
        "ok": bool(result.get("ok")),
        "error": result.get("error", ""),
        "configured": bool(result.get("configured")),
        "errcode": result.get("errcode"),
        "elapsed_ms": result.get("elapsed_ms", 0),
    }


class SettingsBody(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


@router.put("/settings")
async def api_settings_put(body: SettingsBody) -> dict:
    async with session_scope() as session:
        current = await load_app_config(session)
        merged = current.to_dict()
        merged.update({k: v for k, v in body.config.items() if k in merged})
        config = AppConfig.from_dict(merged)
        await save_app_config(session, config)
    get_engine().trigger_now()
    from ..alerts import configure_alerts

    configure_alerts(config)
    return {"ok": True, "config": config.to_dict()}


# --------------------------------------------------------------------------- 机器人连接
class BotCreateBody(BaseModel):
    name: str = ""
    transport: str = "milky"
    mode: str = "builtin"
    backend: str = "yogurt"
    uin: int = 0
    api_url: str = ""
    ws_url: str = ""
    token: str = ""
    protocol_os: str = "Linux"
    protocol_version: str = "fetched"


def _load_sign(session, bot_id: int) -> SignProvider | None:
    return (
        session.execute(
            select(SignProvider).where(SignProvider.bot_connection_id == bot_id).order_by(SignProvider.id.desc())
        )
    ).scalar_one_or_none()


@router.get("/bots")
async def api_bots() -> dict:
    from ..qq.watchdog import watchdog

    async with session_scope() as session:
        bots = (await session.execute(select(BotConnection).order_by(BotConnection.id))).scalars().all()
        payload = []
        for bot in bots:
            sign = (
                await session.execute(
                    select(SignProvider)
                    .where(SignProvider.bot_connection_id == bot.id)
                    .order_by(SignProvider.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            item = _bot(bot, sign)
            item["health"] = watchdog.status(bot.id)
            payload.append(item)
    return {"ok": True, "bots": payload, "backends": [
        {"key": key, "label": spec.label, "requires_sign": spec.requires_sign, "notes": spec.notes}
        for key, spec in BACKENDS.items()
    ], "hosted_backends": hosted_manager.backends()}


@router.post("/bots")
async def api_bots_create(body: BotCreateBody) -> dict:
    if body.transport not in ("milky", "onebot11"):
        raise HTTPException(400, "transport 只能是 milky 或 onebot11")
    if body.mode not in ("builtin", "external", "hosted"):
        raise HTTPException(400, "mode 只能是 builtin、external 或 hosted")
    if body.mode == "builtin":
        if body.transport != "milky":
            raise HTTPException(400, "内置模式只支持 Milky")
        if body.uin <= 0:
            raise HTTPException(400, "内置模式必须填 QQ 号")
        if body.backend not in BACKENDS:
            raise HTTPException(400, f"不支持的内置协议端：{body.backend}")
    elif body.mode == "hosted":
        if body.backend not in HOSTED_BACKENDS:
            raise HTTPException(400, f"不支持的托管后端：{body.backend}")
        if body.uin <= 0:
            raise HTTPException(400, "托管模式必须填 QQ 号")
        body.transport = "onebot11"
    else:
        if body.transport == "onebot11" and not body.ws_url:
            raise HTTPException(400, "OneBot11 外接模式需要 ws_url")
        if body.transport == "milky" and not body.api_url:
            raise HTTPException(400, "Milky 外接模式需要 api_url")

    async with session_scope() as session:
        bot = BotConnection(
            name=body.name.strip() or (f"QQ {body.uin}" if body.uin else "新的机器人"),
            transport=body.transport,
            mode=body.mode,
            backend=body.backend
            if body.mode in ("builtin", "hosted")
            else ("onebot11" if body.transport == "onebot11" else "milky"),
            uin=body.uin,
            api_url=body.api_url,
            ws_url=body.ws_url,
            token=body.token,
            protocol_os=body.protocol_os,
            protocol_version=body.protocol_version,
            state="stopped",
        )
        session.add(bot)
        await session.flush()
        bot_id = bot.id
        payload = _bot(bot)
    return {"ok": True, "bot": payload, "id": bot_id}


class BotPatchBody(BaseModel):
    name: str | None = None
    enabled: bool | None = None


@router.patch("/bots/{bot_id}")
async def api_bots_patch(bot_id: int, body: BotPatchBody) -> dict:
    async with session_scope() as session:
        bot = await session.get(BotConnection, bot_id)
        if bot is None:
            raise HTTPException(404, "连接不存在")
        if body.name is not None:
            bot.name = body.name
        if body.enabled is not None:
            bot.enabled = body.enabled
        bot.updated_at = now_ts()
        return {"ok": True, "bot": _bot(bot)}


@router.delete("/bots/{bot_id}")
async def api_bots_delete(bot_id: int) -> dict:
    await login_manager.stop(bot_id)
    await registry.manager.stop(bot_id)
    async with session_scope() as session:
        bot = await session.get(BotConnection, bot_id)
        if bot is None:
            raise HTTPException(404, "连接不存在")
        await session.delete(bot)
    return {"ok": True}


# --------------------------------------------------------------------------- 扫码登录
@router.post("/bots/{bot_id}/login/start")
async def api_login_start(bot_id: int) -> dict:
    try:
        snapshot = await login_manager.start(bot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "login": snapshot}


@router.post("/bots/{bot_id}/login/restart")
async def api_login_restart(bot_id: int) -> dict:
    try:
        snapshot = await login_manager.restart(bot_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "login": snapshot}


@router.post("/bots/{bot_id}/login/stop")
async def api_login_stop(bot_id: int) -> dict:
    await login_manager.stop(bot_id)
    return {"ok": True}


@router.get("/bots/{bot_id}/login")
async def api_login_status(bot_id: int) -> dict:
    snapshot = login_manager.snapshot(bot_id)
    if snapshot is None:
        async with session_scope() as session:
            bot = await session.get(BotConnection, bot_id)
            if bot is None:
                raise HTTPException(404, "连接不存在")
            return {"ok": True, "login": {"conn_id": bot_id, "state": bot.state, "qr": None, "error": bot.last_error, "done": bot.state == "logged_in"}}
    return {"ok": True, "login": snapshot}


@router.get("/bots/{bot_id}/login/log")
async def api_login_log(bot_id: int, lines: int = 80) -> dict:
    return {"ok": True, "log": login_manager.log_tail(bot_id, max(10, min(lines, 300)))}


# --------------------------------------------------------------------------- 托管协议端
@router.get("/hosted/backends")
async def api_hosted_backends() -> dict:
    return {"ok": True, "backends": hosted_manager.backends()}


class HostedInstallBody(BaseModel):
    bot_id: int


@router.post("/hosted/install")
async def api_hosted_install(body: HostedInstallBody) -> dict:
    try:
        job = hosted_manager.install(body.bot_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "job_id": job.id, "job": job.snapshot()}


@router.get("/hosted/install/{job_id}")
async def api_hosted_job(job_id: str) -> dict:
    job = hosted_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在（可能服务重启过）；容器是否已装好请看「状态」")
    return {"ok": True, "job": job.snapshot()}


@router.get("/hosted/{bot_id}/status")
async def api_hosted_status(bot_id: int, request: Request) -> dict:
    host = request.url.hostname or "127.0.0.1"
    try:
        status = await hosted_manager.status(bot_id, host)
    except HostedError as exc:
        raise HTTPException(400, str(exc)) from exc
    job = hosted_manager.job_for_bot(bot_id)
    if job is not None:
        status["job"] = job.snapshot()
    return {"ok": True, "status": status}


class HostedAttachBody(BaseModel):
    ws_url: str = ""
    token: str = ""


class HostedPublicUrlBody(BaseModel):
    url: str = ""


@router.post("/hosted/{bot_id}/public-url")
async def api_hosted_public_url(bot_id: int, body: HostedPublicUrlBody) -> dict:
    """设置这条托管连接 WebUI 的公网地址（如 https://qq.example.com）。"""
    try:
        url = await hosted_manager.set_public_url(bot_id, body.url)
    except HostedError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "webui_public_url": url}


@router.post("/hosted/{bot_id}/attach")
async def api_hosted_attach(bot_id: int, body: HostedAttachBody) -> dict:
    try:
        info = await hosted_manager.attach(bot_id, body.ws_url, body.token)
    except HostedError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "attached": info}


@router.post("/hosted/{bot_id}/start")
async def api_hosted_start(bot_id: int) -> dict:
    try:
        info = await hosted_manager.start(bot_id)
    except (HostedError, Exception) as exc:  # noqa: BLE001
        if isinstance(exc, HostedError):
            raise HTTPException(400, str(exc)) from exc
        raise
    return {"ok": True, "started": info}


@router.post("/hosted/{bot_id}/stop")
async def api_hosted_stop(bot_id: int) -> dict:
    try:
        info = await hosted_manager.stop(bot_id)
    except HostedError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **info}


@router.post("/hosted/{bot_id}/remove")
async def api_hosted_remove(bot_id: int) -> dict:
    try:
        info = await hosted_manager.remove(bot_id)
    except HostedError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **info}


# --------------------------------------------------------------------------- 签名
class SignBody(BaseModel):
    bot_connection_id: int | None = None
    kind: str = "selfhosted"
    base_url: str = ""
    token: str = ""
    proxy_url: str = ""
    protocol_version: str = "fetched"


@router.put("/sign")
async def api_sign_put(body: SignBody) -> dict:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(SignProvider)
                .where(SignProvider.bot_connection_id == body.bot_connection_id)
                .order_by(SignProvider.id.desc())
            )
        ).scalar_one_or_none()
        if row is None:
            row = SignProvider(bot_connection_id=body.bot_connection_id)
            session.add(row)
        row.kind = body.kind
        row.base_url = normalize_base(body.base_url)
        row.token = body.token
        row.proxy_url = body.proxy_url
        row.protocol_version = body.protocol_version
        row.updated_at = now_ts()
        await session.flush()
        return {
            "ok": True,
            "sign": {
                "kind": row.kind,
                "base_url": row.base_url,
                "token": row.token,
                "proxy_url": row.proxy_url,
                "protocol_version": row.protocol_version,
            },
        }


class SignCheckBody(BaseModel):
    bot_connection_id: int | None = None
    base_url: str = ""
    token: str = ""
    proxy_url: str = ""
    protocol_version: str = ""


@router.post("/sign/check")
async def api_sign_check(body: SignCheckBody) -> dict:
    base_url, token, proxy, version = body.base_url, body.token, body.proxy_url, body.protocol_version
    if not base_url and body.bot_connection_id:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(SignProvider)
                    .where(SignProvider.bot_connection_id == body.bot_connection_id)
                    .order_by(SignProvider.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                base_url, token, proxy, version = row.base_url, row.token, row.proxy_url, row.protocol_version or ""
    result = await check_sign(base_url, token, proxy, expected_version=version)
    if body.bot_connection_id:
        async with session_scope() as session:
            rows = (
                await session.execute(select(SignProvider).where(SignProvider.bot_connection_id == body.bot_connection_id))
            ).scalars().all()
            for row in rows:
                row.health = result.health
                row.checked_at = now_ts()
                row.detail = json.dumps(result.detail, ensure_ascii=False, default=str)
                row.updated_at = now_ts()
    return {
        "ok": True,
        "result": {
            "ok": result.ok,
            "health": result.health,
            "message": result.message,
            "platform": result.platform,
            "version": result.version,
            "expected": result.expected,
        },
    }


# --------------------------------------------------------------------------- 手动触发
@router.post("/check")
async def api_check() -> dict:
    get_engine().trigger_now()
    return {"ok": True, "message": "已安排立即检查"}


# --------------------------------------------------------------------------- 签名服务进程
@router.get("/sign/server")
async def api_sign_server_status() -> dict:
    from ..qq.sign_server import sign_server

    return {"ok": True, "status": sign_server.status()}


@router.post("/sign/server/start")
async def api_sign_server_start() -> dict:
    from ..qq.sign_server import sign_server

    return await sign_server.start()


@router.post("/sign/server/stop")
async def api_sign_server_stop() -> dict:
    from ..qq.sign_server import sign_server

    return await sign_server.stop()


# --------------------------------------------------------------------------- 测试推送
class TestPushBody(BaseModel):
    target_id: int


@router.post("/test-push")
async def api_test_push(body: TestPushBody) -> dict:
    from ..qq.message import Message

    async with session_scope() as session:
        target = await session.get(PushTarget, body.target_id)
        if target is None:
            raise HTTPException(404, "推送目标不存在")
        bot = await session.get(BotConnection, target.bot_connection_id)
        if bot is None:
            raise HTTPException(404, "机器人连接不存在")
        client = await registry.manager.get(bot)
        if not client.connected:
            raise HTTPException(503, f"机器人未连接（状态：{client.state}）。先完成扫码登录或检查外接配置。")
        message = Message().text("【livepush 测试消息】\n如果你看到这条，说明推送链路是通的。")
        result = await client.send_message(target.kind, target.target_id, message)
    if not result.ok:
        return {"ok": False, "error": result.error}
    return {"ok": True}


class BindingTestPushBody(BaseModel):
    kind: str = "live_on"


#: 同一绑定 5 秒内只允许发一条测试消息，防止连点刷屏
_BINDING_TEST_AT: dict[int, float] = {}


@router.post("/subscriptions/{binding_id}/test-push")
async def api_binding_test_push(binding_id: int, body: BindingTestPushBody) -> dict:
    """按「该群绑定的直播间」真实数据渲染一条开播/下播消息，末尾注明是测试。"""
    from ..bili.covers import to_base64_uri
    from ..push.render import RenderInput, build_message

    kind = body.kind if body.kind in ("live_on", "live_off") else "live_on"
    now = time.monotonic()
    if now - _BINDING_TEST_AT.get(binding_id, 0.0) < 5.0:
        raise HTTPException(429, "刚发过一条测试消息，5 秒后再试")

    async with session_scope() as session:
        sub = await session.get(Subscription, binding_id)
        if sub is None:
            raise HTTPException(404, "绑定关系不存在")
        room = await session.get(LiveRoom, sub.room_id)
        target = await session.get(PushTarget, sub.target_id)
        if room is None:
            raise HTTPException(404, "直播间不存在")
        if target is None:
            raise HTTPException(404, "推送目标不存在")
        bot = await session.get(BotConnection, target.bot_connection_id)
        if bot is None:
            raise HTTPException(404, "机器人连接不存在")
        config = await load_app_config(session)

    client = await registry.manager.get(bot)
    if not client.connected:
        raise HTTPException(503, f"机器人未连接（状态：{client.state}）。先完成扫码登录或检查外接配置。")

    started = int(getattr(room, "live_started_at", 0) or 0)
    duration = max(0, now_ts() - started) if started else 0
    data = RenderInput(
        kind=kind,
        uname=room.uname or "",
        title=room.title or "",
        area=f"{room.area_parent} / {room.area_name}".strip(" /"),
        url=room.url,
        duration=duration if kind == "live_off" else 0,
        live_started_at=started,
        room_id=room.room_id or 0,
        uid=room.uid or 0,
        include_cover=bool(sub.include_cover),
        at_all=False,  # 测试消息绝不 @全体
        template_override=sub.message_template or "",
    )

    cover_uri: str | None = None
    engine = get_engine()
    if (
        config.cover_mode != "off"
        and sub.include_cover
        and kind == "live_on"
        and room.cover_url
    ):
        if config.cover_mode == "url":
            cover_uri = room.cover_url
        elif engine.bili is not None:
            path = await engine.bili.covers.get(room.cover_url)
            if path is not None:
                try:
                    cover_uri = to_base64_uri(path.read_bytes())
                except OSError:
                    cover_uri = None

    message = build_message(data, config, cover_uri)
    message.text("（测试消息：由 Web UI 手动触发，不是真实的开播/下播）")
    _BINDING_TEST_AT[binding_id] = now
    result = await client.send_message(target.kind, target.target_id, message)
    log.info("手动测试推送：binding=%s kind=%s ok=%s", binding_id, kind, result.ok)
    if not result.ok:
        return {"ok": False, "error": result.error}
    return {"ok": True, "preview": message.preview()}


# --------------------------------------------------------------------------- 诊断
@router.get("/doctor")
async def api_doctor(request: Request) -> dict:
    from ..cli import collect_doctor

    return {"ok": True, "checks": await collect_doctor()}
