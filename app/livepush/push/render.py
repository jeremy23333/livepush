"""把事件渲染成可发送的消息。"""

from __future__ import annotations

from dataclasses import dataclass

from ..appconfig import AppConfig
from ..qq.message import Message
from ..util.timefmt import fmt_duration

DEFAULT_TEMPLATES = {
    "live_on": "{uname} 开播啦！\n分区：{area}\n标题：{title}\n{url}",
    "live_off": "{uname} 下播了\n本次直播时长 {duration}",
    "title_change": "{uname} 直播间标题变更：{title}",
    "area_change": "{uname} 直播间分区变更：{area}",
}


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
    online: int = 0
    room_id: int = 0
    uid: int = 0
    prev_title: str = ""
    include_cover: bool = True
    at_all: bool = False
    template_override: str = ""


def pick_template(kind: str, config: AppConfig, override: str = "") -> str:
    if kind == "live_on" and override.strip():
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
            "online": data.online,
            "room_id": data.room_id,
            "uid": data.uid,
        },
    )

    message = Message()
    if data.at_all and data.kind == "live_on":
        message.mention_all()
    message.text(text)
    # 只在上播/标题变更时带封面；下播带图没有意义还会撑大消息
    if cover_uri and data.include_cover and data.kind in ("live_on", "title_change"):
        message.image(cover_uri)
    return message
