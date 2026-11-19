"""进程内单例持有者（避免各模块互相 import 造成循环）。"""

from __future__ import annotations

from .monitor.engine import MonitorEngine
from .push.dispatcher import Dispatcher

_engine: MonitorEngine | None = None
_dispatcher: Dispatcher | None = None


def set_services(engine: MonitorEngine, dispatcher: Dispatcher) -> None:
    global _engine, _dispatcher
    _engine = engine
    _dispatcher = dispatcher


def get_engine() -> MonitorEngine:
    assert _engine is not None, "监控引擎尚未初始化"
    return _engine


def get_dispatcher() -> Dispatcher:
    assert _dispatcher is not None, "派发器尚未初始化"
    return _dispatcher


def try_get_engine() -> MonitorEngine | None:
    return _engine


def try_get_dispatcher() -> Dispatcher | None:
    return _dispatcher
