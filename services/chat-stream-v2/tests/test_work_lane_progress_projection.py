from work_lane_progress import aggregate_members, lane_progress


def member(status, checked=None, total=None, estimate=None, quality="fresh"):
    return {"status": status, "ac_checked": checked, "ac_total": total, "estimate": estimate,
            "source_changed_at": "2026-01-02T00:00:00.000Z", "observation": {"quality": quality}}


def test_contract_worked_aggregate():
    items = [member("completed", 4, 4, {"p25": 2, "p75": 4, "median": 3}),
             member("in_progress", 1, 3),
             member("in_progress", 0, 2, {"p25": 3, "p75": 5, "median": 4}, "stale"), member("missing")]
    actual = aggregate_members(items, "2026-01-01T00:00:00.000Z")
    assert actual == {"items_total": 4, "items_completed": 1, "items_dropped": 0, "items_open": 2,
                      "items_unresolved": 1, "ac_checked": 5, "ac_total": 9, "ac_members": 3,
                      "open_estimate_h": {"p25": 3, "p75": 5, "median": 4}, "open_estimated": 1,
                      "estimate_complete": False, "freshness_at": "2026-01-02T00:00:00.000Z"}


def test_partition_null_coverage_and_inline_cap():
    items = [member("deprecated"), member("ambiguous"), member("completed"), member("backlog")] * 8
    result = lane_progress({"_members": items, "no_spec_reason": None})
    assert result["items_total"] == sum(result[k] for k in ("items_completed", "items_dropped", "items_open", "items_unresolved"))
    assert result["ac_total"] is None and result["ac_checked"] is None
    assert result["open_estimate_h"] is None and result["open_estimated"] == 0
    assert len(result["members"]) == 8 and result["members_total"] == 32
    assert lane_progress({"_members": items}, all_members=True)["members"] == items


def test_no_open_members_estimate_complete_but_no_numeric_estimate():
    result = aggregate_members([member("completed", 2, 2), member("deprecated")], None)
    assert result["estimate_complete"] is True
    assert result["open_estimate_h"] is None


def test_v2_golden_cells_and_deferred_keys():
    import json
    from pathlib import Path
    from work_lanes_projection import build_frame
    fixture = json.loads((Path(__file__).resolve().parents[3] / "pentacle-chat-core/tests/fixtures/work-lanes-inventory.json").read_text())
    assert fixture["capability"] == "work_lanes_v1" and fixture["fixture_version"] == 2
    assert {cell["name"] for cell in fixture["progress_v2"]} == {
        "active_progress", "paused_leadless", "blocked", "missing_member", "ambiguous_member",
        "missing_estimate", "no_spec", "index_unavailable"}
    for cell in fixture["progress_v2"]:
        actual = build_frame(cell["rows"], {}, now_iso=cell["frame"]["generated_at"], work_index=cell["work_index"])
        assert actual == cell["frame"]
        for lane in actual["lanes"]:
            assert not {"completion_pending", "lead_reported_done", "stale", "remaining_s", "eta_at"} & lane.keys()


def test_increment1_never_infers_completion_from_prose_or_generic_reports():
    """N1 cells retained for increment 2's generation/correlation tests.

    Future expected values: standalone generic done -> null; stale generation
    -> prior false; predecessor report after handoff -> prior true; correlated
    current-generation done -> true. Increment 1 emits none of these R6 keys.
    """
    from work_lanes_projection import project_lane
    cases = [
        ("discussion", "g1", "g1", False, None),
        ("execution", "g2", "g1", False, False),
        ("execution", "g2", "g1", True, True),
        ("execution", "g2", "g2", False, True),
    ]
    for phase, current_generation, report_generation, prior, eventual in cases:
        row = {"lane_id": "wl-demo", "work_state": "paused", "version": 2,
               "phase": phase, "bound_stream_id": "fixture:lead", "bound_generation": current_generation,
               "_lead_row": {"status": "closed", "status_card": {"update": "All done; ready to close."},
                             "_agent_report_status": "done", "report_generation": report_generation},
               "_prior_lead_reported_done": prior, "_members": []}
        projected = project_lane(row, None, "2026-01-01T00:00:00.000Z")
        assert projected["state"] == "paused"
        assert not {"completion_pending", "lead_reported_done", "stale"} & projected.keys()
