"""时间与展示格式工具。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # 优先用系统时区库
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 极端环境兜底
    TZ = timezone(timedelta(hours=8))


def now_ts() -> int:
    import time

    return int(time.time())


def fmt_dt(ts: int | float | None, with_date: bool = True) -> str:
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def fmt_duration(seconds: int | float | None) -> str:
    """把秒数转成「1 小时 3 分钟」这样的中文时长。"""
    total = int(seconds or 0)
    if total < 0:
        total = 0
    if total < 60:
        return f"{total} 秒"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes and not days:
        parts.append(f"{minutes} 分钟")
    if not parts:
        parts.append("不到 1 分钟")
    return " ".join(parts)


def humanize_ago(ts: int | float | None) -> str:
    if not ts:
        return "从未"
    delta = now_ts() - int(ts)
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta} 秒前"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"
