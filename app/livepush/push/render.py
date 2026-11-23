"""把事件渲染成可发送的消息。"""

from __future__ import annotations

from dataclasses import dataclass

from ..appconfig import AppConfig
from ..qq.message import Message
from ..util.timefmt import fmt_dt, fmt_duration

DEFAULT_TEMPLATES = {
    "live_on": "{uname} 开播啦！\n分区：{area}\n标题：{title}\n{url}",
    "live_off": "{uname} 下播了\n本次直播时长 {duration}",
    "title_change": "{uname} 直播间标题变更：{title}",
    "area_change": "{uname} 直播间分区变更：{area}",
    # 手动通知（/rooms 页「通知」按钮）默认文案，占位符与开播推送一致
    "manual": "{uname} 直播通知\n分区：{area}\n标题：{title}\n{url}",
}

# 允许用「自定义文案」渲染的事件类型：开播与手动通知
OVERRIDE_KINDS = ("live_on", "manual")
# 可以带封面图的事件类型：下播带图没有意义还会撑大消息
COVER_KINDS = ("live_on", "title_change", "manual")
# 可以 @全体的事件类型
AT_ALL_KINDS = ("live_on", "manual")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # 模板里写了不存在的占位符时不崩
        return "{" + key + "}"


def render_text(template: str, values: dict) -> str:
    try:
        text = template.format_map(_SafeDict(values))
    except (ValueError, IndexError):
        return template
    return text.strip()


@dataclass(slots=True)
class RenderInput:
    kind: str
    uname: str = ""
    title: str = ""
    area: str = ""
    url: str = ""
    duration: int = 0
    live_started_at: int = 0
    online: int = 0
    room_id: int = 0
    uid: int = 0
    prev_title: str = ""
    include_cover: bool = True
    at_all: bool = False
    template_override: str = ""


def pick_template(kind: str, config: AppConfig, override: str = "") -> str:
    if kind in OVERRIDE_KINDS and override.strip():
        return override
    return getattr(config, f"template_{kind}", "") or DEFAULT_TEMPLATES.get(kind, "{uname}")


def build_message(data: RenderInput, config: AppConfig, cover_uri: str | None) -> Message:
    template = pick_template(data.kind, config, data.template_override)
    text = render_text(
        template,
        {
            "uname": data.uname,
            "title": data.title,
            "prev_title": data.prev_title,
            "area": data.area,
            "url": data.url,
            "duration": fmt_duration(data.duration),
            "duration_sec": data.duration,
            "live_started": fmt_dt(data.live_started_at, with_date=False)
            if data.live_started_at
            else "",
            "online": data.online,
            "room_id": data.room_id,
            "uid": data.uid,
        },
    )

    message = Message()
    if data.at_all and data.kind in AT_ALL_KINDS:
        message.mention_all()
    message.text(text)
    if cover_uri and data.include_cover and data.kind in COVER_KINDS:
        message.image(cover_uri)
    return message
