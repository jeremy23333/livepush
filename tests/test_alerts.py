"""故障告警：冷却去重、每分钟上限、禁用时静默。"""

from __future__ import annotations

import pytest

from livepush.alerts import AlertManager, normalize


def test_normalize_collapses_numbers():
    a = normalize("B 站风控冷却 1800 秒，code=-412")
    b = normalize("B 站风控冷却 60 秒，code=-412")
    assert a == b
    assert normalize("offset 0x6856284") == "offset 0x#"
    assert normalize("offset 0x56D81D1") == "offset 0x#"
    assert len(normalize("x" * 500)) == 120


def _manager(**kwargs) -> AlertManager:
    manager = AlertManager()
    manager.configure(
        enabled=kwargs.get("enabled", True),
        url=kwargs.get("url", "https://example.invalid/hook"),
        min_level=kwargs.get("min_level", "ERROR"),
        cooldown_sec=kwargs.get("cooldown_sec", 600),
        max_per_minute=kwargs.get("max_per_minute", 10),
    )
    return manager


@pytest.mark.asyncio
async def test_cooldown_suppresses_same_key(monkeypatch):
    manager = _manager()
    sent: list[str] = []

    async def fake_send(level: str, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(manager, "_send", fake_send)

    await manager._handle("ERROR", "boom", "k|boom")
    await manager._handle("ERROR", "boom", "k|boom")

    assert len(sent) == 1
    assert manager.sent == 1
    assert manager.suppressed == 1


@pytest.mark.asyncio
async def test_rate_limit_per_minute(monkeypatch):
    manager = _manager(max_per_minute=1)
    sent: list[str] = []

    async def fake_send(level: str, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(manager, "_send", fake_send)

    await manager._handle("ERROR", "first", "a")
    await manager._handle("ERROR", "second", "b")

    assert sent == ["first"]
    assert manager.suppressed == 1


@pytest.mark.asyncio
async def test_disabled_manager_does_nothing(monkeypatch):
    manager = _manager(enabled=False)

    async def fake_send(level: str, text: str) -> bool:  # pragma: no cover
        raise AssertionError("禁用时不应该发送")

    monkeypatch.setattr(manager, "_send", fake_send)
    await manager._handle("CRITICAL", "x", "k")
    assert manager.status()["enabled"] is False


def test_configure_requires_url():
    manager = AlertManager()
    manager.configure(enabled=True, url="  ", min_level="ERROR", cooldown_sec=60, max_per_minute=5)
    assert manager.enabled is False


@pytest.mark.asyncio
async def test_send_test_without_url():
    manager = AlertManager()
    manager.configure(enabled=True, url="", min_level="ERROR", cooldown_sec=60, max_per_minute=5)
    result = await manager.send_test()
    assert result["ok"] is False
    assert "webhook" in result["error"]


def test_status_masks_url():
    manager = _manager()
    status = manager.status()
    assert status["enabled"] is True
    assert status["url"].endswith("/…")
    # 遮盖掉 webhook 的 key 部分
    assert "hook" not in status["url"]
