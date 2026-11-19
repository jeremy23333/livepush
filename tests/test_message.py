"""消息模型与两种协议序列化。"""

from __future__ import annotations

from livepush.qq.message import Message, to_milky, to_onebot11


def test_text_and_image_and_mention():
    msg = Message().text("你好").image("base64://abc")
    assert msg.plain_text() == "你好"
    assert msg.has_image is True
    assert msg.has_mention_all is False


def test_onebot11_segments():
    msg = Message().mention_all().text("开播了").image("base64://abc")
    payload = to_onebot11(msg)
    assert payload[0] == {"type": "at", "data": {"qq": "all"}}
    assert payload[1] == {"type": "text", "data": {"text": "开播了"}}
    assert payload[2] == {"type": "image", "data": {"file": "base64://abc"}}


def test_milky_segments():
    msg = Message().mention_all().text("开播了").image("base64://abc")
    payload = to_milky(msg)
    assert payload[0] == {"type": "mention_all", "data": {}}
    assert payload[1] == {"type": "text", "data": {"text": "开播了"}}
    assert payload[2] == {"type": "image", "data": {"uri": "base64://abc"}}


def test_preview_and_drop_images():
    msg = Message().text("a" * 10).image("base64://x")
    preview = msg.preview(5)
    assert preview.endswith("…")
    dropped = msg.drop_images()
    assert dropped.has_image is False
    assert dropped.plain_text() == "a" * 10
