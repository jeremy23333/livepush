"""协议无关的消息模型（text / image / mention_all）与两个序列化器。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Segment:
    kind: str  # text | image | mention_all
    text: str = ""
    uri: str = ""


@dataclass
class Message:
    segments: list[Segment] = field(default_factory=list)

    # --- 构造 ---
    def text(self, text: str) -> "Message":
        if text:
            self.segments.append(Segment(kind="text", text=text))
        return self

    def image(self, uri: str) -> "Message":
        if uri:
            self.segments.append(Segment(kind="image", uri=uri))
        return self

    def mention_all(self) -> "Message":
        self.segments.append(Segment(kind="mention_all"))
        return self

    # --- 读取 ---
    @property
    def has_image(self) -> bool:
        return any(s.kind == "image" for s in self.segments)

    @property
    def has_mention_all(self) -> bool:
        return any(s.kind == "mention_all" for s in self.segments)

    def plain_text(self) -> str:
        return "".join(s.text for s in self.segments if s.kind == "text")

    def preview(self, limit: int = 160) -> str:
        parts: list[str] = []
        for seg in self.segments:
            if seg.kind == "text":
                parts.append(seg.text)
            elif seg.kind == "image":
                parts.append("[图片]")
            elif seg.kind == "mention_all":
                parts.append("@全体成员")
        text = " ".join(parts).replace("\n", " ⏎ ")
        return text[:limit] + ("…" if len(text) > limit else "")

    def drop_images(self) -> "Message":
        return Message([s for s in self.segments if s.kind != "image"])


def to_onebot11(message: Message) -> list[dict]:
    out: list[dict] = []
    for seg in message.segments:
        if seg.kind == "text":
            out.append({"type": "text", "data": {"text": seg.text}})
        elif seg.kind == "image":
            out.append({"type": "image", "data": {"file": seg.uri}})
        elif seg.kind == "mention_all":
            out.append({"type": "at", "data": {"qq": "all"}})
    return out


def to_milky(message: Message) -> list[dict]:
    out: list[dict] = []
    for seg in message.segments:
        if seg.kind == "text":
            out.append({"type": "text", "data": {"text": seg.text}})
        elif seg.kind == "image":
            out.append({"type": "image", "data": {"uri": seg.uri}})
        elif seg.kind == "mention_all":
            out.append({"type": "mention_all", "data": {}})
    return out
