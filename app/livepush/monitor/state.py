"""直播状态机（纯函数，方便表驱动单测）。

设计要点（借 BililiveRecorder 的容错思路）：
- 轮播（live_status=2）按「未开播」参与状态转换，但数据库保留原值用于展示；
- 下播需要连续 N 次确认，抑制 B 站抖动导致的假下播；
- 首次观测只记快照，默认不推送；
- 采集失败不在这里处理（由调用方保证：失败绝不改成下播）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..bili.parse import RoomSnapshot

EventKind = Literal["live_on", "live_off", "title_change", "area_change"]


@dataclass(slots=True)
class RoomState:
    """参与决策的既有状态。"""

    room_state: str = "new"
    live_status: int = 0
    live_started_at: int = 0
    pending_status: int | None = None
    pending_count: int = 0
    title: str = ""
    area_name: str = ""
    notify_on_add: bool = False

    @property
    def live_norm(self) -> int:
        return 1 if self.live_status == 1 else 0


@dataclass(slots=True)
class Decision:
    live_status: int
    live_started_at: int
    pending_status: int | None
    pending_count: int
    room_state: str
    events: list[EventKind] = field(default_factory=list)
    duration: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.events)


def decide(
    state: RoomState,
    snapshot: RoomSnapshot,
    *,
    now: int,
    on_confirm_polls: int = 1,
    off_confirm_polls: int = 2,
) -> Decision:
    """根据上一状态与新快照，决定是否产生事件。"""
    norm = snapshot.live_norm
    prev_norm = state.live_norm

    # 「首次观测」：从未成功采集过，或之前判定为没有直播间。
    # stale 表示上次采集失败，仍按既有状态继续比较，不当作首见。
    first_time = state.room_state in ("new", "no_room")

    if first_time:
        started = snapshot.live_started_at or (now if norm else 0)
        events: list[EventKind] = []
        if norm and state.notify_on_add:
            events.append("live_on")
        return Decision(
            live_status=snapshot.live_status,
            live_started_at=started,
            pending_status=None,
            pending_count=0,
            room_state="ok",
            events=events,
            duration=0,
        )

    # 状态没变：只可能产生标题/分区变更
    if norm == prev_norm:
        events = []
        if norm == 1:
            if snapshot.title and state.title and snapshot.title != state.title:
                events.append("title_change")
            elif snapshot.area_name and state.area_name and snapshot.area_name != state.area_name:
                events.append("area_change")
        return Decision(
            live_status=snapshot.live_status,
            live_started_at=state.live_started_at,
            pending_status=None,
            pending_count=0,
            room_state="ok",
            events=events,
            duration=0,
        )

    # 状态变化：累计确认次数
    if state.pending_status == norm:
        count = state.pending_count + 1
    else:
        count = 1
    required = on_confirm_polls if norm == 1 else off_confirm_polls
    if count < required:
        return Decision(
            live_status=state.live_status,
            live_started_at=state.live_started_at,
            pending_status=norm,
            pending_count=count,
            room_state="ok",
            events=[],
            duration=0,
        )

    if norm == 1:
        started = snapshot.live_started_at or now
        return Decision(
            live_status=snapshot.live_status,
            live_started_at=started,
            pending_status=None,
            pending_count=0,
            room_state="ok",
            events=["live_on"],
            duration=0,
        )

    duration = max(0, now - state.live_started_at) if state.live_started_at else 0
    return Decision(
        live_status=snapshot.live_status,
        live_started_at=0,
        pending_status=None,
        pending_count=0,
        room_state="ok",
        events=["live_off"],
        duration=duration,
    )
