from work_lane_progress import aggregate_members, lane_progress


def test_reconciliation_intervals_remain_finite_and_bounded():
    import pytest
    from work_lanes_projection import WorkLanesInventory
    for config in ({"sweep_interval_s": float("nan")}, {"sweep_interval_s": float("inf")},
                   {"sweep_interval_s": 0}, {"settle_s": float("inf")}, {"settle_s": -1}):
        with pytest.raises(ValueError, match="work_index_interval_invalid"):
            WorkLanesInventory(None, None, None, **config)


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
        "missing_estimate", "no_spec", "index_unavailable", "stale_observation_completed",
        "completion_pending", "stale_lane", "remaining_only", "elapsed_only_ignored", "umbrella_exempt", "member_conflict"}
    for cell in fixture["progress_v2"]:
        actual = build_frame(cell["rows"], {}, now_iso=cell["frame"]["generated_at"], work_index=cell["work_index"])
        assert actual == cell["frame"]
        for lane in actual["lanes"]:
            assert {"completion_pending", "lead_reported_done", "stale"} <= lane.keys()
            assert not {"remaining_s", "eta_at"} & lane.keys()


def test_increment1_never_infers_completion_from_prose_or_generic_reports():
    """Retain all four Q3 cells, now backed by actual routing/report rows."""
    from test_work_lanes import run
    from test_work_lane_completion import retained_q3_case
    cases = [
        (False, False, False, None),
        (True, True, False, False),
        (True, True, True, True),
        (True, False, False, True),
    ]
    for correlated, stale, prior, expected in cases:
        async def body(env):
            await retained_q3_case(env, correlated=correlated, stale=stale, prior=prior, expected=expected)
        run(body)


def test_four_a1_cells_use_real_parser_projection_offline_and_conflict(tmp_path):
    import json
    from pathlib import Path
    import pytest
    from _shared.specs_parser import parse_work_facts
    from test_work_lane_remaining_estimate import offline, manifest
    from test_work_lane_progress import a1_snapshot
    from test_work_lanes import run
    fixture = json.loads((Path(__file__).resolve().parents[3] / "pentacle-chat-core/tests/fixtures/work-lanes-inventory.json").read_text())
    expected = {"remaining_only": ({"p25": 3, "p75": 7, "median": 5}, 2, True),
                "elapsed_only_ignored": (None, 0, False),
                "umbrella_exempt": ({"p25": 2, "p75": 4, "median": 3}, 1, True),
                "member_conflict": ({"p25": 2, "p75": 4, "median": 3}, 1, True)}
    assert set(expected) <= {cell["name"] for cell in fixture["progress_v2"]}
    for cell in fixture["progress_v2"]:
        if cell["name"] not in expected:
            continue
        root = tmp_path / cell["name"]
        row = cell["rows"][0]
        for source, member in zip(cell["sources"], row["_members"]):
            facts = parse_work_facts(source["spec"], source["summary"], source["status"])
            assert all(member[k] == value for k, value in facts.items())
            folder = root / "work" / source["status"] / source["spec_id"]
            folder.mkdir(parents=True)
            (folder / "spec.md").write_text(source["spec"])
            (folder / "summary.md").write_text(source["summary"])
        actual = aggregate_members(row["_members"], row["updated_at"])
        assert tuple(actual[k] for k in ("open_estimate_h", "open_estimated", "estimate_complete")) == expected[cell["name"]]
        result = offline(root, manifest(*[m["spec_id"] for m in row["_members"]]))
        assert result.returncode == 0, result.stderr
        lane = json.loads(result.stdout)["lanes"][0]
        assert tuple(lane[k] for k in ("open_estimate_h", "open_estimated", "estimate_complete")) == expected[cell["name"]]
        assert lane["members"] == [{k: member[k] for k in ("spec_id", "status", "estimate", "estimate_exempt")}
                                    for member in row["_members"]]
        if cell["name"] == "member_conflict":
            async def body(env):
                setup, attempt = cell["setup"], cell["attempt"]
                holder = (await env.adopt(setup["adoption_key"], state="paused", owner="operator", members=setup["members"]))["lane"]
                assert holder["lane_id"] == cell["expected_error"]["lane_id"]
                target = (await env.adopt("request:fixture-conflict-target", state="paused", owner="operator"))["lane"]
                before = await a1_snapshot(env)
                with pytest.raises(ValueError, match=cell["expected_error"]["error_code"]) as error:
                    await env.op(attempt["operation"], {"members": attempt["members"]}, lane=target["lane_id"], version=1)
                assert holder["lane_id"] in str(error.value)
                assert await a1_snapshot(env) == before
            run(body)
