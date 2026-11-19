"""模板渲染。"""

from __future__ import annotations

from livepush.appconfig import AppConfig
from livepush.push.render import RenderInput, build_message, pick_template, render_text


def test_default_live_on_template():
    config = AppConfig()
    data = RenderInput(kind="live_on", uname="主播A", title="标题", area="生活 / 日常", url="https://live.bilibili.com/1")
    message = build_message(data, config, cover_uri=None)
    text = message.plain_text()
    assert "主播A 开播啦！" in text
    assert "生活 / 日常" in text
    assert "https://live.bilibili.com/1" in text


def test_unknown_placeholder_is_kept():
    assert render_text("hi {uname}{nope}", {"uname": "X"}) == "hi X{nope}"


def test_broken_brace_does_not_crash():
    assert render_text("hi {uname", {"uname": "X"}) == "hi {uname"


def test_offline_message_has_duration_and_no_cover():
    config = AppConfig()
    data = RenderInput(kind="live_off", uname="主播", duration=3725, include_cover=True)
    message = build_message(data, config, cover_uri="base64://x")
    assert "1 小时 2 分钟" in message.plain_text()
    assert message.has_image is False  # 下播不带图


def test_at_all_only_for_live_on():
    config = AppConfig()
    on = build_message(RenderInput(kind="live_on", uname="A", at_all=True), config, None)
    off = build_message(RenderInput(kind="live_off", uname="A", at_all=True), config, None)
    assert on.has_mention_all is True
    assert off.has_mention_all is False


def test_cover_attached_for_live_on_and_title_change():
    config = AppConfig()
    on = build_message(RenderInput(kind="live_on", uname="A"), config, "base64://x")
    title = build_message(RenderInput(kind="title_change", uname="A"), config, "base64://x")
    area = build_message(RenderInput(kind="area_change", uname="A"), config, "base64://x")
    assert on.has_image is True
    assert title.has_image is True
    assert area.has_image is False


def test_subscription_template_override_applies_to_live_on_only():
    config = AppConfig()
    override = "自定义：{uname} 来了"
    on = build_message(RenderInput(kind="live_on", uname="A", template_override=override), config, None)
    off = build_message(RenderInput(kind="live_off", uname="A", template_override=override), config, None)
    assert on.plain_text() == "自定义：A 来了"
    assert "下播" in off.plain_text()


def test_pick_template_reads_config():
    config = AppConfig(template_live_off="X{duration}")
    assert pick_template("live_off", config) == "X{duration}"
    assert pick_template("live_on", config, "OVERRIDE") == "OVERRIDE"


def test_include_cover_false_drops_image():
    config = AppConfig()
    message = build_message(RenderInput(kind="live_on", uname="A", include_cover=False), config, "base64://x")
    assert message.has_image is False
