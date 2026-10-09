"""R6 lane facts and durable, silent-clear episodes on disposable stores."""
import pytest

from work_lane_progress import lane_progress


NOW = "2026-01-03T00:00:00.000Z"
OLD = "2026-01-01T00:00:00.000Z"


def member(status="completed", quality="fresh"):
    return {"spec_id": "spec_demo__span", "status": status,
            "terminal": status if status in ("completed", "deprecated") else None,
            "ac_checked": None, "ac_total": None, "estimate": None,
            "source_changed_at": OLD, "observation": {"quality": quality}, "obs_rev": 1}


@pytest.mark.parametrize("members,available,state,expected", [
    ([member()], True, "paused", True),
    ([member(), member("deprecated")], True, "blocked", True),
    ([member("deprecated")], True, "paused", False),
    ([], True, "paused", False),
    ([member(), member("in_progress")], True, "paused", False),
    ([member(), member("missing", "missing")], True, "paused", False),
    ([member(), member("ambiguous", "ambiguous")], True, "paused", False),
    ([member(quality="stale")], True, "paused", None),
    ([member(quality="error")], True, "paused", None),
    ([member()], False, "paused", None),
    ([member()], True, "done", False),
    ([member(quality="stale")], False, "done", False),
    ([member(), member("in_progress"), member("in_progress", "stale"), member("missing", "missing")],
     True, "paused", None),
])
def test_completion_pending_tristate(members, available, state, expected):
    result = lane_progress({"_members": members, "_work_index_available": available,
                            "work_state": state})
    assert result["completion_pending"] is expected
    assert result["lead_reported_done"] is None


@pytest.mark.parametrize("state,qualifies,stamp,expected", [
    ("active", True, OLD, True),
    ("active", False, OLD, False),
    ("paused", True, OLD, False),
    ("blocked", True, OLD, False),
    ("done", True, OLD, False),
    ("active", True, "2026-01-02T00:00:00.000Z", False),
    ("active", True, NOW, False),
    ("active", True, None, False),
])
def test_stale_uses_presented_active_and_strict_threshold(state, qualifies, stamp, expected):
    result = lane_progress({"work_state": state, "_qualifies": qualifies,
                            "_fd_updated_at": stamp}, now_iso=NOW)
    assert result["stale"] is expected


def test_stale_threshold_configuration(monkeypatch):
    monkeypatch.setenv("WORK_LANE_STALE_H", "72")
    row = {"work_state": "active", "_qualifies": True, "_fd_updated_at": OLD}
    assert lane_progress(row, now_iso=NOW)["stale"] is False
    monkeypatch.setenv("WORK_LANE_STALE_H", "12")
    assert lane_progress(row, now_iso=NOW)["stale"] is True


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "bad"])
def test_stale_threshold_rejects_invalid_configuration(monkeypatch, value):
    monkeypatch.setenv("WORK_LANE_STALE_H", value)
    with pytest.raises(ValueError, match="work_lane_stale_interval_invalid"):
        lane_progress({"work_state": "paused"}, now_iso=NOW)
