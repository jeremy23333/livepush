"""状态机表驱动测试。"""

from __future__ import annotations

from livepush.bili.parse import RoomSnapshot
from livepush.monitor.state import RoomState, decide

NOW = 1_700_000_000


def snap(live_status: int = 1, started: int = 0, title: str = "标题", area: str = "分区") -> RoomSnapshot:
    return RoomSnapshot(
        uid=42,
        room_id=100,
        uname="主播",
        title=title,
        area_name=area,
        live_status=live_status,
        live_started_at=started,
    )


def test_first_sight_offline_never_pushes():
    decision = decide(RoomState(room_state="new"), snap(0), now=NOW)
    assert decision.events == []
    assert decision.live_status == 0
    assert decision.room_state == "ok"


def test_first_sight_live_is_silent_by_default():
    decision = decide(RoomState(room_state="new"), snap(1, NOW - 500), now=NOW)
    assert decision.events == []
    assert decision.live_status == 1
    assert decision.live_started_at == NOW - 500


def test_first_sight_live_with_notify_on_add():
    state = RoomState(room_state="new", notify_on_add=True)
    decision = decide(state, snap(1, NOW - 500), now=NOW)
    assert decision.events == ["live_on"]


def test_goes_live():
    state = RoomState(room_state="ok", live_status=0)
    decision = decide(state, snap(1, NOW - 10), now=NOW, on_confirm_polls=1)
    assert decision.events == ["live_on"]
    assert decision.live_started_at == NOW - 10


def test_live_started_at_falls_back_to_now():
    state = RoomState(room_state="ok", live_status=0)
    decision = decide(state, snap(1, 0), now=NOW)
    assert decision.events == ["live_on"]
    assert decision.live_started_at == NOW


def test_goes_offline_needs_two_confirmations():
    state = RoomState(room_state="ok", live_status=1, live_started_at=NOW - 3600)
    first = decide(state, snap(0), now=NOW, off_confirm_polls=2)
    assert first.events == []
    assert first.pending_status == 0
    assert first.pending_count == 1
    assert first.live_status == 1  # 尚未确认，保持原状态

    state2 = RoomState(
        room_state="ok",
        live_status=first.live_status,
        live_started_at=first.live_started_at,
        pending_status=first.pending_status,
        pending_count=first.pending_count,
    )
    second = decide(state2, snap(0), now=NOW + 30, off_confirm_polls=2)
    assert second.events == ["live_off"]
    assert second.duration == 3630
    assert second.live_started_at == 0


def test_flap_resets_pending_counter():
    state = RoomState(room_state="ok", live_status=1, live_started_at=NOW - 100, pending_status=0, pending_count=1)
    decision = decide(state, snap(1), now=NOW, off_confirm_polls=2)
    assert decision.events == []
    assert decision.pending_status is None
    assert decision.pending_count == 0


def test_carousel_status_2_counts_as_offline():
    state = RoomState(room_state="ok", live_status=0)
    decision = decide(state, snap(2), now=NOW)
    assert decision.events == []
    assert decision.live_status == 2  # 原值保留用于展示
    assert decision.room_state == "ok"


def test_carousel_ends_a_live_session():
    state = RoomState(room_state="ok", live_status=1, live_started_at=NOW - 600)
    first = decide(state, snap(2), now=NOW, off_confirm_polls=1)
    assert first.events == ["live_off"]
    assert first.duration == 600
    assert first.live_status == 2


def test_title_change_only_while_live():
    live_state = RoomState(room_state="ok", live_status=1, title="旧标题", live_started_at=NOW - 100)
    assert decide(live_state, snap(1, NOW - 100, title="新标题"), now=NOW).events == ["title_change"]

    offline_state = RoomState(room_state="ok", live_status=0, title="旧标题")
    assert decide(offline_state, snap(0, 0, title="新标题"), now=NOW).events == []


def test_area_change_when_title_same():
    state = RoomState(room_state="ok", live_status=1, title="标题", area_name="旧分区", live_started_at=NOW - 10)
    decision = decide(state, snap(1, NOW - 10, title="标题", area="新分区"), now=NOW)
    assert decision.events == ["area_change"]


def test_stale_state_continues_comparison():
    state = RoomState(room_state="stale", live_status=1, live_started_at=NOW - 10)
    decision = decide(state, snap(0), now=NOW, off_confirm_polls=1)
    assert decision.events == ["live_off"]
