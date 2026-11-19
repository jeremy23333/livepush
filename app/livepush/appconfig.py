"""运行期可改配置（存 SQLite 的 app_setting 表）。

和 `config.Settings` 的分工：
- Settings：基础设施与密钥（数据目录、监听地址、口令），来自环境变量；
- AppConfig：业务参数（轮询间隔、模板、Cookie、封面模式），来自 Web UI。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AppSetting

SETTING_KEY = "app_config"


@dataclass
class AppConfig:
    # 轮询
    poll_interval_live: int = 30
    poll_interval_idle: int = 90
    poll_interval_night: int = 120
    night_start_hour: int = 2
    night_end_hour: int = 8
    jitter_ratio: float = 0.2
    chunk_size: int = 50
    chunk_delay_ms: int = 400

    # 状态机去抖
    on_confirm_polls: int = 1
    off_confirm_polls: int = 2
    title_debounce_sec: int = 120
    min_event_interval_sec: int = 60

    # 推送
    max_event_age_hours: int = 6
    cover_mode: str = "base64"  # base64 | url | off
    at_all_default: bool = False

    # B 站
    bili_cookie: str = ""
    bili_proxy: str = ""

    # 模板
    template_live_on: str = "{uname} 开播啦！\n分区：{area}\n标题：{title}\n{url}"
    template_live_off: str = "{uname} 下播了\n本次直播时长 {duration}"
    template_title_change: str = "{uname} 直播间标题变更：{title}"

    def clamp(self) -> "AppConfig":
        """把明显不合理的值收回到安全范围。"""
        self.poll_interval_live = max(10, min(int(self.poll_interval_live), 600))
        self.poll_interval_idle = max(10, min(int(self.poll_interval_idle), 1800))
        self.poll_interval_night = max(10, min(int(self.poll_interval_night), 3600))
        self.chunk_size = max(1, min(int(self.chunk_size), 100))
        self.chunk_delay_ms = max(0, min(int(self.chunk_delay_ms), 5000))
        self.on_confirm_polls = max(1, min(int(self.on_confirm_polls), 5))
        self.off_confirm_polls = max(1, min(int(self.off_confirm_polls), 10))
        self.title_debounce_sec = max(0, min(int(self.title_debounce_sec), 3600))
        self.min_event_interval_sec = max(0, min(int(self.min_event_interval_sec), 3600))
        self.max_event_age_hours = max(1, min(int(self.max_event_age_hours), 72))
        self.night_start_hour = max(0, min(int(self.night_start_hour), 23))
        self.night_end_hour = max(0, min(int(self.night_end_hour), 23))
        self.jitter_ratio = max(0.0, min(float(self.jitter_ratio), 1.0))
        if self.cover_mode not in ("base64", "url", "off"):
            self.cover_mode = "base64"
        return self

    def interval_for(self, any_live: bool, hour: int) -> int:
        """按是否有在播、是否夜间决定本轮间隔。"""
        in_night = (
            self.night_start_hour <= hour < self.night_end_hour
            if self.night_start_hour <= self.night_end_hour
            else (hour >= self.night_start_hour or hour < self.night_end_hour)
        )
        if in_night:
            return self.poll_interval_night
        return self.poll_interval_live if any_live else self.poll_interval_idle

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        cfg = cls(**clean)
        return cfg.clamp()


async def load_app_config(session: AsyncSession) -> AppConfig:
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY))
    ).scalar_one_or_none()
    if row is None or not row.value:
        return AppConfig()
    try:
        return AppConfig.from_dict(json.loads(row.value))
    except (ValueError, TypeError):
        return AppConfig()


async def save_app_config(session: AsyncSession, config: AppConfig) -> AppConfig:
    config.clamp()
    payload = json.dumps(config.to_dict(), ensure_ascii=False)
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY))
    ).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=SETTING_KEY, value=payload))
    else:
        row.value = payload
    await session.flush()
    return config


async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    return row.value if row is not None else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.flush()
