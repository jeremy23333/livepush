"""Web 层公共依赖：模板引擎、静态资源、通用过滤器。"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .. import __version__
from ..util.timefmt import fmt_dt, fmt_duration, humanize_ago

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["dt"] = fmt_dt
templates.env.filters["duration"] = fmt_duration
templates.env.filters["ago"] = humanize_ago

STATE_LABELS = {
    "stopped": "已停止",
    "preparing": "准备中",
    "launching": "启动中",
    "generating_qr": "生成二维码",
    "qr_ready": "等待扫码",
    "logged_in": "已连接",
    "reusing_session": "验证旧登录态",
    "expired": "二维码过期",
    "error": "异常",
    "connected": "已连接",
    "disconnected": "已断开",
    "connecting": "连接中",
}

ROOM_STATE_LABELS = {
    "new": "待观测",
    "ok": "正常",
    "no_room": "无直播间",
    "stale": "采集异常",
}

JOB_STATUS_LABELS = {
    "pending": "待发送",
    "sending": "发送中",
    "sent": "已发送",
    "failed": "失败",
    "skipped": "已跳过",
}

EVENT_LABELS = {
    "live_on": "开播",
    "live_off": "下播",
    "title_change": "标题变更",
    "area_change": "分区变更",
    "manual": "手动通知",
}

templates.env.globals.update(
    STATE_LABELS=STATE_LABELS,
    ROOM_STATE_LABELS=ROOM_STATE_LABELS,
    JOB_STATUS_LABELS=JOB_STATUS_LABELS,
    EVENT_LABELS=EVENT_LABELS,
    # 静态资源版本号：Cloudflare / 浏览器会长期缓存 app.js，
    # 不加版本号时新增的按钮在旧 JS 里没有监听器，表现为「点了没反应」。
    version=__version__,
)
