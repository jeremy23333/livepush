"""ORM 表定义。

约定：
- 时间一律存 UTC unix 秒（Integer），展示层再转 Asia/Shanghai。
- QQ 号 / 群号存 TEXT，避免超过 2^53 后在 JS 与 JSON 里丢精度。
- JSON 用 TEXT 存，读写时自行 json.dumps/loads。
"""

from __future__ import annotations

from time import time as _time

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_ts() -> int:
    return int(_time())


class Base(DeclarativeBase):
    pass


class LiveRoom(Base):
    __tablename__ = "live_room"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    room_id: Mapped[int] = mapped_column(BigInteger, default=0)
    short_id: Mapped[int] = mapped_column(BigInteger, default=0)
    uname: Mapped[str] = mapped_column(String(128), default="")
    face: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    area_name: Mapped[str] = mapped_column(String(64), default="")
    area_parent: Mapped[str] = mapped_column(String(64), default="")
    cover_url: Mapped[str] = mapped_column(Text, default="")

    live_status: Mapped[int] = mapped_column(Integer, default=0)  # 原始 0/1/2
    live_started_at: Mapped[int] = mapped_column(Integer, default=0)
    pending_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)

    # new=尚未观测到 / ok=正常 / no_room=该 UID 没有直播间 / stale=连续采集失败
    room_state: Mapped[str] = mapped_column(String(16), default="new")
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[int] = mapped_column(Integer, default=0)
    last_ok_at: Mapped[int] = mapped_column(Integer, default=0)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_add: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ts)

    @property
    def live_norm(self) -> int:
        """归一化状态：轮播(2) 视作未开播。"""
        return 1 if self.live_status == 1 else 0

    @property
    def url(self) -> str:
        rid = self.room_id or self.short_id or self.uid
        return f"https://live.bilibili.com/{rid}"


class BotConnection(Base):
    __tablename__ = "bot_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    transport: Mapped[str] = mapped_column(String(16), default="milky")  # milky | onebot11
    mode: Mapped[str] = mapped_column(String(16), default="builtin")  # builtin | external
    backend: Mapped[str] = mapped_column(String(32), default="yogurt")  # yogurt|lagrange_milky|llbot|waylay|onebot11
    api_url: Mapped[str] = mapped_column(String(256), default="")
    ws_url: Mapped[str] = mapped_column(String(256), default="")
    token: Mapped[str] = mapped_column(String(200), default="")
    workdir: Mapped[str] = mapped_column(String(512), default="")
    qr_source: Mapped[str] = mapped_column(String(16), default="auto")  # auto|file|url|pty

    # 内置协议端相关
    uin: Mapped[int] = mapped_column(BigInteger, default=0)
    http_port: Mapped[int] = mapped_column(Integer, default=0)
    protocol_os: Mapped[str] = mapped_column(String(16), default="Linux")
    protocol_version: Mapped[str] = mapped_column(String(32), default="fetched")
    self_id: Mapped[int] = mapped_column(BigInteger, default=0)
    nickname: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(24), default="stopped")
    last_error: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ts)


class HostedEngine(Base):
    """我们托管的「外部协议端」实例（NapCat / SnowLuma 的 Docker 容器）。

    BotConnection 仍然是唯一对外的连接实体；这张表只记录容器/端口/产物这些
    「怎么把它跑起来」的细节，删连接时级联删除。
    """

    __tablename__ = "hosted_engine"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_connection_id: Mapped[int] = mapped_column(
        ForeignKey("bot_connection.id", ondelete="CASCADE"), unique=True, index=True
    )
    backend: Mapped[str] = mapped_column(String(32), default="napcat")  # napcat | snowluma
    runtime: Mapped[str] = mapped_column(String(16), default="docker")  # docker | native
    image_ref: Mapped[str] = mapped_column(String(256), default="")
    container_name: Mapped[str] = mapped_column(String(128), default="")
    install_dir: Mapped[str] = mapped_column(String(512), default="")
    artifact_path: Mapped[str] = mapped_column(String(512), default="")
    artifact_version: Mapped[str] = mapped_column(String(64), default="")
    host_ws_port: Mapped[int] = mapped_column(Integer, default=0)
    host_http_port: Mapped[int] = mapped_column(Integer, default=0)
    host_webui_port: Mapped[int] = mapped_column(Integer, default=0)
    remote_port: Mapped[int] = mapped_column(Integer, default=0)
    token: Mapped[str] = mapped_column(String(200), default="")
    state: Mapped[str] = mapped_column(String(24), default="pending")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ts)


class SignProvider(Base):
    __tablename__ = "sign_provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("bot_connection.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="selfhosted")  # none|selfhosted|remote
    base_url: Mapped[str] = mapped_column(String(256), default="")
    token: Mapped[str] = mapped_column(String(200), default="")
    proxy_url: Mapped[str] = mapped_column(String(256), default="")
    protocol_version: Mapped[str] = mapped_column(String(32), default="fetched")
    health: Mapped[str] = mapped_column(String(16), default="unknown")  # ok|unreachable|mismatch|unauthorized|unknown
    checked_at: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ts)


class PushTarget(Base):
    __tablename__ = "push_target"
    __table_args__ = (UniqueConstraint("bot_connection_id", "kind", "target_id", name="uq_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_connection_id: Mapped[int] = mapped_column(ForeignKey("bot_connection.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(8), default="group")  # group | private
    target_id: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ts)


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = (UniqueConstraint("room_id", "target_id", name="uq_subscription"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("live_room.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("push_target.id", ondelete="CASCADE"), index=True)
    notify_live_on: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_live_off: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_title_change: Mapped[bool] = mapped_column(Boolean, default=False)
    at_all: Mapped[bool] = mapped_column(Boolean, default=False)
    include_cover: Mapped[bool] = mapped_column(Boolean, default=True)
    message_template: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts)


class LiveEvent(Base):
    __tablename__ = "live_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("live_room.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # live_on|live_off|title_change|area_change
    prev_status: Mapped[int] = mapped_column(Integer, default=0)
    new_status: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[int] = mapped_column(Integer, default=0)  # 仅 live_off
    snapshot: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts, index=True)


class PushJob(Base):
    __tablename__ = "push_job"
    __table_args__ = (
        UniqueConstraint("event_id", "target_id", name="uq_job"),
        Index("ix_job_pending", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("live_event.id", ondelete="CASCADE"))
    target_id: Mapped[int] = mapped_column(ForeignKey("push_target.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|sending|sent|failed|skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    preview: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ts)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ts)


class LoginSession(Base):
    __tablename__ = "login_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_connection_id: Mapped[int] = mapped_column(ForeignKey("bot_connection.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(24), default="idle")
    qr_sha1: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[int] = mapped_column(Integer, default=now_ts)
    ended_at: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ts)


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[int] = mapped_column(Integer, default=now_ts)
