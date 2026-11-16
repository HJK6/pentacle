"""Lane detail projection and display-only enrichment on the real lane store."""
import asyncio
import json
import threading
from pathlib import Path

import pytest

from _shared.lane_icons import LANE_ICON_KEYS, _warn_unknown, resolve_icon
from work_lane_worked import WorkedCache, read_worked, worked_from_time
from work_lanes_projection import attribute_members, build_frame, project_lane
from test_work_lanes import run
from test_usage_rollup import Fx, NOW, out
from tools import usage_rollup as ur


SPEC = "spec_demo__detail"


def lane(members=None, **extra):
    return {"lane_id": "wl-detail", "title": "Mobile release", "version": 1,
            "work_state": "paused", "_members": members or [], **extra}


def member(**extra):
    return {"spec_id": SPEC, "status": "in_progress", "status_text": "Tests pass. Next sentence.",
            "next_action_text": "Review the result.", "ac_total": 1, "ac_checked": 0,
            "criteria": [{"title": "Check `code`", "disposition": "open"}],
            "transitions": [{"status": "in_progress", "at": "2026-01-01"}], **extra}


def test_icons_explicit_order_fallback_unknown_once(caplog):
    assert len(LANE_ICON_KEYS) == 24
    assert resolve_icon("cube", "Mobile release") == "cube"
    assert resolve_icon(None, "Mobile release") == "phone"
    assert resolve_icon(None, "Unmatched project") is None
    _warn_unknown.cache_clear()
    for _ in range(3):
        assert resolve_icon("legacy-value", "Mobile") is None
    assert sum("unknown legacy work lane icon" in record.message for record in caplog.records) == 1


def test_icon_set_text_validates_and_persists_in_existing_operation():
    async def body(env):
        stored = (await env.adopt(state="paused", owner="operator"))["lane"]
        for invalid in ("no-such-icon", "", 7, {}, ["phone"]):
            with pytest.raises(ValueError, match="work_lane_icon_invalid"):
                await env.op("set_text", {"icon": invalid}, lane=stored["lane_id"], version=stored["version"])
        changed = await env.op("set_text", {"icon": "cube"}, lane=stored["lane_id"], version=stored["version"])
        assert changed["lane"]["icon"] == "cube"
        shown = await env.store.get_work_lane(stored["lane_id"])
        assert shown["events"][0]["operation"] == "adopt"
        assert shown["events"][-1]["operation"] == "set_text"
        cleared = await env.op("set_text", {"icon": None}, lane=stored["lane_id"], version=changed["lane"]["version"])
        assert cleared["lane"]["icon"] is None
    run(body)


def test_icon_survives_actual_host_custody_transfer_and_restart(tmp_path):
    from test_assistant_recovery_command import fixture, OLD, CHAT
    from test_host_transfer_authority import claim_bundle, prepare_bundle
    async def body():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, *_):
            old = await store.fetch_session(*OLD.split(":", 1))
            def seed(conn):
                conn.execute("INSERT INTO v2_assistant_composite_lanes "
                             "(lane_id,stream_id,phase,bound_stream_id,bound_generation,title,icon,work_state,"
                             "owner_kind,created_at,updated_at,no_spec_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             ("wl-icon-transfer", CHAT, "execution", OLD, old["session_generation"], "Mobile", "phone",
                              "paused", "fd", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "Fixture transfer"))
                conn.commit()
            await store.submit(seed)
            command, *_ = await claim_bundle(store, server, gateway, composite)
            parts = await prepare_bundle(store, server, command)
            result = await server.host_transfer.commit(command)
            assert result["state"] == "committed"
            target = next(part["target"] for part in parts if part["source"] == OLD)
            store.stop()
            store.start()
            shown = await store.get_work_lane("wl-icon-transfer")
            assert shown["lane"]["icon"] == "phone" and shown["lane"]["bound_stream_id"] == target
    asyncio.run(body())


@pytest.mark.parametrize("roles,expected", [(["qa"], None), ([], None), (["nexus", "qa"], "nexus"),
                                          (["nexus", "lead", "worker"], "lead")])
def test_lead_selection_uses_role_order_and_status_card(roles, expected):
    sessions = {f"host-a:{role}": {"status": "open", "role": role, "host": "host-a", "provider": "codex",
                                 "assigned_spec_id": SPEC, "online": True, "working": True,
                                 "status_card": {"eta_at": "2026-01-02T00:00:00Z",
                                                 "plan": [{"status": "active", "text": "Review"}]}}
                for role in roles}
    lead = attribute_members([member()], sessions, None)[0]["lead_seat"]
    assert lead == (None if expected is None else {
        "stream_id": f"host-a:{expected}", "host": "host-a", "provider": "codex", "activity": "working",
        "eta_at": "2026-01-02T00:00:00Z", "active_step": "Review"})


def test_inventory_omits_detail_arrays_but_show_keeps_all_members():
    raw = lane([member(spec_id=f"spec_demo__detail{i}") for i in range(27)])
    inventory = build_frame([raw], {}, now_iso="2026-01-02T00:00:00Z")["lanes"][0]
    shown = project_lane(raw, None, "2026-01-02T00:00:00Z", all_members=True)
    assert inventory["icon"] == "phone" and len(inventory["members"]) == 8 and len(shown["members"]) == 27
    for item in inventory["members"]:
        assert "criteria" not in item and "transitions" not in item
        assert item["status_line"] == "Tests pass. Next sentence." and item["next_line"] == "Review the result."
        assert "blocked_line" not in item
        assert item["worked"] is None and item["lead_seat"] is None
    assert all(item["criteria"] == member()["criteria"] and item["transitions"] == member()["transitions"]
               for item in shown["members"])


def test_structured_wire_fixture_matches_both_surfaces():
    fixture = json.loads((Path(__file__).resolve().parents[3] /
                          "pentacle-chat-core/tests/fixtures/work-lanes-inventory.json").read_text())["spec_details_v1"]
    frame = build_frame([fixture["lane"]], fixture["sessions"], now_iso=fixture["now_iso"],
                        worked_by_spec=fixture["worked_by_spec"])
    shown = project_lane(fixture["lane"], None, fixture["now_iso"], all_members=True,
                         sessions_by_stream=fixture["sessions"], worked_by_spec=fixture["worked_by_spec"])
    # Preserve the frozen older cell while checking the one additive empty-road key.
    assert frame["lanes"][0].pop("goal_road") == ""
    assert shown.pop("goal_road") == ""
    assert frame == fixture["inventory_frame"]
    assert shown == fixture["show_projection"]


def test_real_index_show_and_material_journal(tmp_path, monkeypatch):
    from _shared.specs_service import SpecsSubsystem
    from sessions import Sessions
    from server import Server
    from work_lanes_projection import WorkLanesInventory
    root = tmp_path / "memory"
    folder = root / "work/in_progress/demo__detail"
    folder.mkdir(parents=True)
    path = folder / "spec.md"
    path.write_text(f"---\nid: {SPEC}\ncreated_at: '2026-01-01'\n---\n"
                    "## Tracking\n- summary transition: 2026-01-03; source lifecycle: backlog; moved.\n"
                    "## Acceptance Criteria\n### Custom\n- [ ] **First check** — details.\n")
    folder.joinpath("summary.md").write_text("**Status** — Tests pass. Review pending\n**Next action** — Review the result.\n")
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    scanner = SpecsSubsystem(memory_root=root, session_summaries=lambda: [], changed_callback=lambda _: None)
    async def body(env):
        stored = (await env.adopt(state="paused", owner="operator", members=[SPEC]))["lane"]
        now = max(path.stat().st_mtime, folder.joinpath("summary.md").stat().st_mtime) + 10
        await env.store.reconcile_work_observations(scanner.scan_work_observations(now=now), settle_s=0, sweep=True)
        sessions = Sessions(env.store, tmux=None, local_host="host-a")
        await sessions.refresh()
        server = Server(store=env.store, sessions=sessions, comms=None, local_host="host-a")
        server.work_lanes = WorkLanesInventory(env.store, sessions, server.broadcast)
        operator = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
        shown = await server._on_work_lanes_show({"lane_id": stored["lane_id"], "_auth_context": operator})
        item = shown["members"][0]
        assert item["criteria"] == [{"title": "First check", "disposition": "open"}]
        assert item["transitions"] == [{"status": "backlog", "at": "2026-01-01"}, {"status": "in_progress", "at": "2026-01-03"}]
        assert item["status_line"] == "Tests pass. Review pending"
        inventory = await server.work_lanes.current()
        assert "criteria" not in inventory["lanes"][0]["members"][0]
        assert "transitions" not in inventory["lanes"][0]["members"][0]
        for offset, (before, after) in enumerate((("First check", "Second check"), ("2026-01-03", "2026-01-04")), 1):
            path.write_text(path.read_text().replace(before, after))
            await env.store.reconcile_work_observations(scanner.scan_work_observations(now=now + offset), settle_s=0, sweep=True)
        shown = await env.store.get_work_lane(stored["lane_id"])
        assert shown["members"][0]["obs_rev"] == item["obs_rev"] + 2
        assert len([event for event in shown["events"] if event["operation"] == "item_change"]) == 2
        changed = await env.op("set_text", {"icon": "phone"}, lane=stored["lane_id"], version=shown["lane"]["version"])
        server.work_lanes.worked.values = {SPEC: {"elapsed_h": 1, "censored": True, "active_h": 0.5,
                                                   "seats": 1, "qa_rounds": 0, "as_of": "2026-01-04T00:00:00Z"}}
        projected = await server.work_lanes.current()
        assert projected["lanes"][0]["icon"] == "phone"
        assert projected["lanes"][0]["members"][0]["worked"]["active_h"] == 0.5
        final = await env.store.get_work_lane(changed["lane"]["lane_id"])
        assert final["members"][0]["obs_rev"] == shown["members"][0]["obs_rev"]
        assert len([event for event in final["events"] if event["operation"] == "item_change"]) == 2
    run(body)


def test_lead_step_has_a_120_character_cap():
    sessions = {"host-a:lead": {"assigned_spec_id": SPEC, "role": "lead", "host": "host-a",
                                "status": "open", "status_card": {"plan": [{"status": "active", "text": "Z" * 121}]}}}
    assert attribute_members([member()], sessions, None)[0]["lead_seat"]["active_step"] == "Z" * 119 + "…"


def test_background_batch_covers_members_of_done_lanes(tmp_path):
    from types import SimpleNamespace
    from test_work_lane_progress import a1_close
    from work_lanes_projection import WorkLanesInventory
    seen = []
    worked = {"elapsed_h": 2, "censored": False, "active_h": 0.5,
              "seats": 1, "qa_rounds": 1, "as_of": "2026-01-04T00:00:00Z"}
    def reader(specs, *_):
        seen.append(specs)
        return {spec: worked for spec in specs}
    async def body(env):
        stored = (await env.adopt(state="paused", owner="operator", members=[SPEC]))["lane"]
        await a1_close(env, stored)
        async def broadcast(_):
            pass
        inventory = WorkLanesInventory(env.store, SimpleNamespace(list_open=lambda: []), broadcast, refresh_min_s=0)
        inventory.worked = WorkedCache(tmp_path, tmp_path, changed=lambda: None, reader=reader)
        try:
            inventory.refresh()
            await inventory._task
            if inventory.worked._task is not None:
                await inventory.worked._task
            assert seen == [{SPEC}]
            assert (await inventory.current())["lanes"] == []
            history = await inventory.list(include_done=True)
            assert history["lanes"][0]["members"][0]["worked"] == worked
        finally:
            await inventory.stop()
    run(body)


BUDGET = json.loads((Path(__file__).parent / "fixtures/work_lane_byte_budget.json").read_text())


def _budget_frame(count):
    # Synthetic text at measured live p95 lengths. One real assigned lead per
    # member produces BOTH the legacy seat and the new lead_seat, without any
    # post-projection field replacement. New fields use their agreed caps.
    measured = BUDGET["measurement"]
    rows, sessions, worked_by_spec = [], {}, {}
    for lane_index in range(count):
        members = []
        for i in range(8):
            identity = f"spec_demo__detail{lane_index}-{i}"
            stream_id = f"host-a:lead{lane_index}-{i}"
            members.append(member(spec_id=identity, title="Z" * measured["p95_member_title_chars"],
                                  terminal=None, estimate=None, estimate_exempt=False,
                                  status_text="Z" * 159 + ".",
                                  next_action_text="Y" * 159 + ".",
                                  source_changed_at="2026-01-01T00:00:00Z", obs_rev=1,
                                  observation={"quality": "fresh", "observed_at": "2026-01-01T00:00:00Z", "error": None}))
            sessions[stream_id] = {
                "assigned_spec_id": identity, "role": "lead", "host": "host-a", "status": "open",
                "provider": "codex", "online": True, "visibility": "hidden",
                "parent_stream_id": "host-a:" + "Z" * (41 - len(stream_id) + len("host-a:lead0")),
                "status_card": {"goal": "Z" * 293, "eta_at": "2026-01-03T00:00:00Z",
                                "plan": [{"status": "active", "text": "Z" * 120}]}}
            worked_by_spec[identity] = {"elapsed_h": 100.5, "censored": True, "active_h": 10.25,
                                       "seats": 8, "qa_rounds": 4, "as_of": "2026-01-02T00:00:00Z"}
        rows.append(lane(members, lane_id=f"wl-{lane_index}", title="Z" * measured["p95_lane_title_chars"],
                         icon="phone", bound_stream_id=f"host-a:lead{lane_index}-0", _qualifies=True))
    return build_frame(rows, sessions, worked_by_spec=worked_by_spec, now_iso="2026-01-02T00:00:00Z")


def test_per_member_new_field_delta_at_caps():
    frame = _budget_frame(1)
    lane_row = frame["lanes"][0]
    compact_size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    assert compact_size(lane_row["lead"]["status_card"]) == BUDGET["measurement"]["p95_status_card_json_bytes"]
    for item in lane_row["members"]:
        assert len(item["status_line"]) == len(item["next_line"]) == 160
        assert len(item["lead_seat"]["active_step"]) == 120 and item["worked"]
        assert len(item["seats"]) == 1
        assert compact_size(item["seats"][0]) == BUDGET["measurement"]["p95_seat_json_bytes"]
        prior = {key: value for key, value in item.items() if key not in {"worked", "lead_seat", "status_line", "next_line"}}
        assert compact_size(item) - compact_size(prior) <= BUDGET["member_new_field_bytes"]
        assert "criteria" not in item and "transitions" not in item


@pytest.mark.parametrize("cell", ["representative", "stress"])
def test_inventory_byte_budget_uses_real_correlated_projection(cell):
    from server import _encode_frame
    case = BUDGET[cell]
    frame = _budget_frame(case["lanes"])
    assert len(frame["lanes"]) == case["lanes"]
    assert len({member["spec_id"] for lane in frame["lanes"] for member in lane["members"]}) == case["lanes"] * case["members"]
    assert all(len(lane["members"]) == case["members"] for lane in frame["lanes"])
    assert len(_encode_frame(frame).encode()) <= case["max_bytes"]


def test_batched_worked_equals_rollup_on_real_ledger(tmp_path, monkeypatch):
    fx = Fx(tmp_path)
    try:
        fx.seat("host-a:lead", created="2026-09-01T00:00:00Z", specs=(SPEC,))
        fx.rec("host-a:lead", out(1), observed="2026-09-01T00:00:00Z", host="host-a")
        fx.rec("host-a:lead", out(2), observed="2026-09-01T00:05:00Z", host="host-a")
        fx.report("host-a:lead", "accept", "2026-09-01T00:06:00Z")
        expected = fx.run("--spec", SPEC)["specs"][0]["time"]
        calls = []
        original = ur.Sources
        def source(**kwargs):
            calls.append(1)
            return original(**kwargs)
        monkeypatch.setattr(ur, "Sources", source)
        actual = read_worked({SPEC, "spec_demo__absent"}, fx.data, fx.work, ur.parse_ts(NOW))
        assert len(calls) == 1
        assert actual[SPEC] == worked_from_time(expected, NOW)
        assert actual[SPEC]["active_h"] == 0.0833 and actual[SPEC]["censored"] is True
        assert actual["spec_demo__absent"] is None
    finally:
        fx.conn.close()


def test_worked_null_proxies():
    metrics = {"elapsed_delivery_h": {"censored": False, "value_h": 4}, "activity_proxy_h": {"value": None},
               "codex_activity_proxy_h": {"value": None}, "seats": 2, "qa_rounds": 1}
    assert worked_from_time(metrics, "now")["active_h"] is None
    metrics["codex_activity_proxy_h"]["value"] = 0.25
    metrics["activity_proxy_h"]["value"] = 0.5
    assert worked_from_time(metrics, "now")["active_h"] == 0.75


def test_cache_ttl_off_loop_timeout_and_missing_ledger(tmp_path):
    async def body():
        clock = [0.0]
        calls = []
        loop_thread = threading.get_ident()
        def reader(specs, *_):
            calls.append((set(specs), threading.get_ident()))
            return {spec: {"test": True} for spec in specs}
        cache = WorkedCache(tmp_path, tmp_path, changed=lambda: None, reader=reader, clock=lambda: clock[0])
        cache.refresh({SPEC, "spec_demo__other"})
        await cache._task
        cache.refresh({SPEC})
        assert len(calls) == 1 and calls[0][0] == {SPEC, "spec_demo__other"} and calls[0][1] != loop_thread
        clock[0] = 600
        cache.refresh({SPEC})
        await cache._task
        assert len(calls) == 2
        await cache.stop()

        entered, release = threading.Event(), threading.Event()
        def slow(*_):
            entered.set()
            release.wait(2)
            return {SPEC: {"late": True}}
        cache = WorkedCache(tmp_path, tmp_path, changed=lambda: None, reader=slow, timeout_s=0.01, clock=lambda: clock[0])
        cache.refresh({SPEC})
        await cache._task
        assert entered.is_set() and cache.values == {SPEC: None}
        first = cache._worker
        clock[0] += 600
        cache.refresh({SPEC})
        assert cache._worker is first  # no overlapping uncancellable read
        release.set()
        await cache._worker
        assert cache.values == {SPEC: None}  # late data never commits
        await cache.stop()

        missing = WorkedCache(tmp_path / "absent", tmp_path, changed=lambda: None)
        missing.refresh({SPEC})
        await missing._task
        assert missing.values == {SPEC: None}
        await missing.stop()
    asyncio.run(body())


def test_appended_lane_icons_preserve_existing_keyword_precedence():
    from _shared.lane_icons import validate_icon, resolve_icon
    for key in ('briefcase', 'headset', 'film', 'server'):
        assert validate_icon(key) == key
    for title, expected in [('Business acquisition', 'briefcase'), ('Customer support', 'headset'),
                            ('Animation', 'film'), ('Hosts and accounts', 'server'),
                            ('Pentacle mobile', 'phone'), ('Altum CRM and phone', 'phone'),
                            ('Government contracts', 'columns')]:
        assert resolve_icon(None, title) == expected
