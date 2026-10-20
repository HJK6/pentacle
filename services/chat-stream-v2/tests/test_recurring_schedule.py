"""Durable recurring schedules: rules, materialisation, miss/overlap policy, transport."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import uuid

import pytest

import recurrence
from sessions import VerbError
from window_schedule import RECURRING_LATE_FIRE_GRACE_S, WindowSchedule

from test_window_schedule_contract import (
    SPEC, FakeSpawn, LinkCheckingSpawn, auth, harness, message, run,
)

UTC = timezone.utc
OCCURRENCE = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)  # 02:00 America/Chicago (CDT)
OCCURRENCE_ISO = "2026-10-21T07:00:00.000000Z"


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def operator_message(verb: str, **values) -> dict:
    return {
        "type": verb, "request_id": str(uuid.uuid4()), "from_stream_id": "operator",
        "_auth_context": auth("operator", role="operator"), **values,
    }


def create_rule(surface, actor: str = "hosta:requester", **values) -> dict:
    payload = {
        "name": "Nightly retro", "every": "daily", "at_time": "02:00", "provider": "codex",
        "model": "gpt-5.6-sol", "effort": "high", "spec_ids": [SPEC], "target_host": "hosta",
        "initial_prompt": "Run the retro and report.", "objective": "Nightly retro",
        "parent": "none",
    }
    payload.update(values)
    return run(surface.recurring_create(message("schedule.recurring.create", actor, **payload)))["recurring"]


def force_next(store, recurring_id: str, value: str, **extra) -> None:
    sets = ",".join(["next_fire_at_utc=?", *(f"{key}=?" for key in extra)])
    run(store.submit(lambda conn: (
        conn.execute(
            f"UPDATE v2_recurring_schedules SET {sets} WHERE recurring_id=?",
            (value, *extra.values(), recurring_id),
        ), conn.commit(),
    )))


def rule_row(store, recurring_id: str) -> dict:
    return run(store.submit(lambda conn: dict(conn.execute(
        "SELECT * FROM v2_recurring_schedules WHERE recurring_id=?", (recurring_id,),
    ).fetchone())))


def occurrence_rows(store) -> list[dict]:
    return run(store.submit(lambda conn: [dict(row) for row in conn.execute(
        "SELECT * FROM v2_schedules WHERE owner_service_actor='recurring' ORDER BY fires_at_utc",
    ).fetchall()]))


def pass_at(surface, moment: datetime) -> int:
    return run(surface.materialise_due_recurring(now=moment))


# ---- recurrence math --------------------------------------------------------

def test_next_occurrence_daily_weekdays_weekly_and_default_zone() -> None:
    daily = {"every": "daily", "at_time": "02:00"}
    assert recurrence.normalize(daily)["tz"] == "America/Chicago"
    assert recurrence.next_occurrence(daily, datetime(2026, 10, 20, 12, tzinfo=UTC)) == OCCURRENCE
    assert recurrence.next_occurrence(daily, OCCURRENCE) == OCCURRENCE + timedelta(days=1)
    weekdays = {"every": "weekdays", "at_time": "08:00", "tz": "America/Chicago"}
    friday_after = datetime(2026, 10, 23, 14, tzinfo=UTC)  # Fri 09:00 CDT, past 08:00
    assert recurrence.next_occurrence(weekdays, friday_after) == datetime(2026, 10, 26, 13, tzinfo=UTC)
    weekly = {"every": "weekly", "days": "mon,wed", "at_time": "08:00", "tz": "UTC"}
    assert recurrence.next_occurrence(weekly, datetime(2026, 10, 20, 14, tzinfo=UTC)) == datetime(
        2026, 10, 21, 8, tzinfo=UTC)


def test_dst_gap_resolves_to_first_valid_instant_and_overlap_to_first_occurrence() -> None:
    # 2026-03-08: 02:00-03:00 America/Chicago does not exist (08:00Z is the transition).
    gap = {"every": "daily", "at_time": "02:30", "tz": "America/Chicago"}
    assert recurrence.next_occurrence(gap, datetime(2026, 3, 7, 12, tzinfo=UTC)) == datetime(
        2026, 3, 8, 8, 0, tzinfo=UTC)
    assert recurrence.next_occurrence({**gap, "at_time": "02:00"}, datetime(2026, 3, 7, 12, tzinfo=UTC)) == datetime(
        2026, 3, 8, 8, 0, tzinfo=UTC)
    # 2026-11-01: 01:30 happens twice; the first (CDT, 06:30Z) wins.
    overlap = {"every": "daily", "at_time": "01:30", "tz": "America/Chicago"}
    assert recurrence.next_occurrence(overlap, datetime(2026, 10, 31, 12, tzinfo=UTC)) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC)
    assert recurrence.next_occurrence(overlap, datetime(2026, 11, 1, 6, 30, tzinfo=UTC)) == datetime(
        2026, 11, 2, 7, 30, tzinfo=UTC)


@pytest.mark.parametrize("rule, code", [
    ({"every": "daily", "at_time": "02:00", "tz": "Mars/Olympus"}, "invalid_timezone"),
    ({"every": "hourly", "at_time": "02:00"}, "invalid_recurrence"),
    ({"every": "daily", "at_time": "25:00"}, "invalid_recurrence"),
    ({"every": "weekly", "at_time": "02:00"}, "invalid_recurrence"),
    ({"every": "daily", "at_time": "02:00", "days": ["mon"]}, "invalid_recurrence"),
])
def test_invalid_rules_are_refused_at_create(tmp_path, rule, code) -> None:
    store, _s, _c, spawn, surface = harness(tmp_path)
    try:
        with pytest.raises(VerbError) as exc:
            create_rule(surface, **{"every": "daily", "at_time": "02:00", **rule})
        assert exc.value.code == code
        assert spawn.admissions == []
    finally:
        store.stop()


# ---- verbs, snapshot and frames ---------------------------------------------

def test_recurring_verbs_snapshot_and_lifecycle_frames(tmp_path) -> None:
    pushed: list[dict] = []

    async def broadcast(frame: dict) -> None:
        pushed.append(frame)

    store, _s, _c, spawn, surface = harness(tmp_path, broadcast=broadcast)
    try:
        created = create_rule(surface, tz="America/Chicago")
        rid = created["recurring_id"]
        assert uuid.UUID(rid).version == 4
        assert created["state"] == "active" and created["tz"] == "America/Chicago"
        assert created["next_fire_at_utc"] and created["next_fire_local"].endswith(("-05:00", "-06:00"))
        assert "prompt_b64" not in created and created["prompt_preview"] == "Run the retro and report."
        assert [row["recurring_id"] for row in run(surface.recurring_inventory())] == [rid]

        got = run(surface.recurring_get(message("schedule.recurring.get", "hosta:requester", recurring_id=rid)))
        assert base64.b64decode(got["recurring"]["initial_prompt_b64"]).decode() == "Run the retro and report."
        listed = run(surface.recurring_list(message("schedule.recurring.list", "hosta:requester")))
        assert [row["recurring_id"] for row in listed["recurring"]] == [rid]

        admissions_before = len(spawn.admissions)
        run(surface.recurring_update(operator_message(
            "schedule.recurring.update", recurring_id=rid, name="Early retro", at_time="03:30")))
        assert len(spawn.admissions) == admissions_before  # operator Edit never re-runs spawn admission
        run(surface.recurring_pause(operator_message("schedule.recurring.pause", recurring_id=rid)))
        assert rule_row(store, rid)["state"] == "paused"
        run(surface.recurring_resume(operator_message("schedule.recurring.resume", recurring_id=rid)))
        assert rule_row(store, rid)["state"] == "active"
        run(surface.recurring_delete(operator_message("schedule.recurring.delete", recurring_id=rid)))
        assert run(surface.recurring_inventory()) == []

        assert [frame["event"] for frame in pushed] == [
            "recurring_created", "recurring_updated", "recurring_paused",
            "recurring_resumed", "recurring_deleted",
        ]
        assert all(frame["type"] == "schedule.lifecycle" and frame["recurring"]["recurring_id"] == rid
                   for frame in pushed)
        assert pushed[1]["recurring"]["name"] == "Early retro"
        assert pushed[1]["recurring"]["recurrence"]["at_time"] == "03:30"
    finally:
        store.stop()


def test_template_update_reruns_admission_and_name_update_does_not(tmp_path) -> None:
    store, _s, _c, spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        before = len(spawn.admissions)
        updated = run(surface.recurring_update(message(
            "schedule.recurring.update", "hosta:requester", recurring_id=rid, model="gpt-5.6-luna")))
        assert len(spawn.admissions) == before + 1
        assert updated["recurring"]["model"] == "gpt-5.6-luna"
        assert rule_row(store, rid)["generation"] == 2
        with pytest.raises(VerbError) as exc:
            run(surface.recurring_update(message(
                "schedule.recurring.update", "hosta:requester", recurring_id=rid,
                name="x", expected_generation=1)))
        assert exc.value.code == "generation_conflict"
    finally:
        store.stop()


def test_snapshot_carries_recurring_key_beside_schedules(tmp_path) -> None:
    from server import Server
    from test_schedule_transport import AcceptingRegistry, _authenticated_hello

    store, sessions, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        server = Server()
        server.window_schedule = surface
        server.operator_credential_registry = AcceptingRegistry()
        websocket = object()
        snapshot = run(server._on_hello(_authenticated_hello(server, websocket)))[1]
        assert [row["recurring_id"] for row in snapshot["recurring"]] == [rid]
        assert snapshot["schedules"] == []
    finally:
        store.stop()


# ---- materialisation --------------------------------------------------------

def test_materialise_is_idempotent_across_passes_and_restart_and_copies_the_template(tmp_path) -> None:
    store, sessions, comms, spawn, surface = harness(tmp_path)
    try:
        created = create_rule(surface, visibility="hidden", role="worker", self_close_on_completion=True,
                              parent="hosta:participant")
        rid = created["recurring_id"]
        force_next(store, rid, OCCURRENCE_ISO)
        assert pass_at(surface, OCCURRENCE + timedelta(seconds=5)) == 1
        assert pass_at(surface, OCCURRENCE + timedelta(seconds=5)) == 0  # already advanced
        # Simulated restart replaying the same instant (previous row already terminal, so the
        # overlap rule is not what stops a second row: the UNIQUE request_id is).
        run(store.submit(lambda conn: (
            conn.execute("UPDATE v2_schedules SET state='cancelled',terminal_at=updated_at"), conn.commit())))
        force_next(store, rid, OCCURRENCE_ISO)
        restarted = WindowSchedule(store, sessions, comms, spawn, local_host="hosta")
        restarted.mark_store_ready()
        pass_at(restarted, OCCURRENCE + timedelta(seconds=6))
        rows = occurrence_rows(store)
        assert len(rows) == 1
        row = rows[0]
        assert row["request_id"] == str(uuid.uuid5(uuid.UUID(rid), OCCURRENCE_ISO))
        assert row["owner_service_actor"] == "recurring" and row["owner_stream_id"] is None
        assert row["created_by_stream_id"] == "hosta:requester"
        assert row["fires_at_utc"] == OCCURRENCE_ISO
        assert (row["resolved_provider"], row["resolved_model"], row["resolved_effort"]) == (
            "codex", "gpt-5.6-sol", "high")
        assert row["visibility"] == "hidden" and row["role"] == "worker"
        assert row["parent_stream_id"] == "hosta:participant"
        assert row["objective"] == "Nightly retro" and row["self_close_on_completion"] == 1
        assert row["owner_spec_ids_json"] == f'["{SPEC}"]'
        assert base64.b64decode(row["prompt_b64"]).decode() == "Run the retro and report."
        receipt = run(store.submit(lambda conn: dict(conn.execute(
            "SELECT * FROM v2_operation_receipts WHERE request_id=? AND phase='row_committed'",
            (row["request_id"],)).fetchone())))
        assert (receipt["surface"], receipt["verb"], receipt["actor_kind"], receipt["actor_id"],
                receipt["target_id"]) == (
            "schedule", "schedule.recurring.materialise", "service", "recurring", row["schedule_id"])
        after = rule_row(store, rid)
        assert after["last_schedule_id"] == row["schedule_id"] and after["last_fired_at_utc"] == OCCURRENCE_ISO
        assert after["missed_count"] == 0
        assert len(spawn.admissions) == 1  # admission ran at create only
    finally:
        store.stop()


def test_late_fire_inside_grace_and_single_miss_beyond_it(tmp_path) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        force_next(store, rid, OCCURRENCE_ISO)
        pass_at(surface, OCCURRENCE + timedelta(seconds=RECURRING_LATE_FIRE_GRACE_S - 1))
        assert len(occurrence_rows(store)) == 1 and rule_row(store, rid)["missed_count"] == 0

        # Down for three days: exactly one miss, nothing fires, no backlog replay.
        force_next(store, rid, OCCURRENCE_ISO)
        woke = OCCURRENCE + timedelta(days=3, seconds=1)
        pass_at(surface, woke)
        state = rule_row(store, rid)
        assert len(occurrence_rows(store)) == 1
        assert state["missed_count"] == 1 and state["last_missed_reason"] == "late"
        assert state["last_missed_at"] == iso(woke)
        assert datetime.fromisoformat(state["next_fire_at_utc"].replace("Z", "+00:00")) > woke
        pass_at(surface, woke + timedelta(seconds=1))
        assert rule_row(store, rid)["missed_count"] == 1

        force_next(store, rid, OCCURRENCE_ISO)
        pass_at(surface, OCCURRENCE + timedelta(seconds=RECURRING_LATE_FIRE_GRACE_S + 1))
        assert rule_row(store, rid)["missed_count"] == 2
        assert len(occurrence_rows(store)) == 1
    finally:
        store.stop()


def test_recover_applies_the_same_evaluation_to_due_rules(tmp_path) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        force_next(store, rid, iso(datetime.now(UTC) - timedelta(hours=2)))
        run(surface.recover())
        state = rule_row(store, rid)
        assert state["missed_count"] == 1 and occurrence_rows(store) == []
    finally:
        store.stop()


def test_paused_rule_accrues_no_miss_and_resume_recomputes_without_one(tmp_path) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        run(surface.recurring_pause(message("schedule.recurring.pause", "hosta:requester", recurring_id=rid)))
        force_next(store, rid, OCCURRENCE_ISO)
        pass_at(surface, OCCURRENCE + timedelta(days=2))
        assert rule_row(store, rid)["missed_count"] == 0 and occurrence_rows(store) == []
        run(surface.recurring_resume(message("schedule.recurring.resume", "hosta:requester", recurring_id=rid)))
        state = rule_row(store, rid)
        assert state["missed_count"] == 0 and state["state"] == "active"
        assert datetime.fromisoformat(state["next_fire_at_utc"].replace("Z", "+00:00")) > datetime.now(UTC)
    finally:
        store.stop()


def test_overlap_skips_while_previous_row_or_child_seat_is_open_and_run_is_refused(tmp_path) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path, spawn_response={
        "type": "spawn.ok", "stream_id": "hosta:recurring-child",
        "initial_prompt_delivery": {"state": "delivered"},
    })
    try:
        rid = create_rule(surface)["recurring_id"]
        force_next(store, rid, OCCURRENCE_ISO)
        pass_at(surface, OCCURRENCE)
        first = occurrence_rows(store)[0]
        # Non-terminal previous row (still pending): the next occurrence is skipped.
        next_due = OCCURRENCE + timedelta(days=1)
        force_next(store, rid, iso(next_due))
        pass_at(surface, next_due)
        state = rule_row(store, rid)
        assert state["missed_count"] == 1 and state["last_missed_reason"] == "previous_run_open"
        assert len(occurrence_rows(store)) == 1
        with pytest.raises(VerbError) as exc:
            run(surface.recurring_run(message("schedule.recurring.run", "hosta:requester", recurring_id=rid)))
        assert exc.value.code == "previous_run_open"

        # Fired, child seat still open: still open.
        run(surface._fire_schedule(first["schedule_id"]))
        run(store.open_session("hosta", "recurring-child", provider="codex", created_at=iso(OCCURRENCE)))
        assert rule_row(store, rid)["last_schedule_id"] == first["schedule_id"]
        after = next_due + timedelta(days=1)
        force_next(store, rid, iso(after))
        pass_at(surface, after)
        assert rule_row(store, rid)["missed_count"] == 2 and len(occurrence_rows(store)) == 1
        with pytest.raises(VerbError) as exc:
            run(surface.recurring_run(message("schedule.recurring.run", "hosta:requester", recurring_id=rid)))
        assert exc.value.code == "previous_run_open"

        # Seat closed: the next occurrence materialises, and so does Run now afterwards.
        run(store.submit(lambda conn: (
            conn.execute("UPDATE sessions SET status='closed' WHERE session_name='recurring-child'"),
            conn.commit())))
        later = after + timedelta(days=1)
        force_next(store, rid, iso(later))
        pass_at(surface, later)
        assert len(occurrence_rows(store)) == 2
    finally:
        store.stop()


def test_run_materialises_immediately_without_moving_the_schedule(tmp_path) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        armed = rule_row(store, rid)["next_fire_at_utc"]
        result = run(surface.recurring_run(message("schedule.recurring.run", "hosta:requester", recurring_id=rid)))
        rows = occurrence_rows(store)
        assert [row["schedule_id"] for row in rows] == [result["schedule_id"]]
        assert rule_row(store, rid)["next_fire_at_utc"] == armed
    finally:
        store.stop()


def test_assistant_parent_resolves_at_materialisation_and_unbound_is_skipped(tmp_path) -> None:
    store, sessions, _c, spawn, surface = harness(tmp_path, spawn_response={
        "type": "spawn.ok", "stream_id": "hosta:recurring-child",
        "initial_prompt_delivery": {"state": "delivered"},
    })
    try:
        sessions.rows["hosta:front"] = {
            "stream_id": "hosta:front", "status": "open", "role": "assistant",
            "qualified_spec_ids": [SPEC], "spec_binding_provenance": [],
        }
        bound: list[str | None] = [None]

        async def binding() -> str | None:
            return bound[0]

        surface.assistant_binding = binding
        rid = create_rule(surface, parent="assistant", visibility="hidden")["recurring_id"]
        force_next(store, rid, OCCURRENCE_ISO)
        pass_at(surface, OCCURRENCE)
        state = rule_row(store, rid)
        assert state["missed_count"] == 1 and state["last_missed_reason"] == "parent_unbound"
        assert occurrence_rows(store) == []

        bound[0] = "hosta:front"
        later = OCCURRENCE + timedelta(days=1)
        force_next(store, rid, iso(later))
        pass_at(surface, later)
        (row,) = occurrence_rows(store)
        assert row["parent_stream_id"] == "hosta:front"
        run(surface._fire_schedule(row["schedule_id"]))  # service-owned fire path
        call = spawn.calls[-1]
        assert call["parent_stream_id"] == "hosta:front"
        assert call["_auth_context"]["service_actor"] == "daemon:scheduler"
    finally:
        store.stop()


def test_rule_whose_spec_is_no_longer_lane_linked_still_materialises(tmp_path) -> None:
    store, sessions, comms, _spawn, _surface = harness(tmp_path)
    spawn = LinkCheckingSpawn(linked={SPEC})
    surface = WindowSchedule(store, sessions, comms, spawn, local_host="hosta")
    surface.mark_store_ready()
    try:
        sessions.rows["hosta:requester"]["assigned_spec_id"] = SPEC
        rid = create_rule(surface)["recurring_id"]
        checks = len(spawn.checked)
        assert checks >= 1
        spawn.linked.clear()  # the spec's lane closed
        force_next(store, rid, OCCURRENCE_ISO)
        pass_at(surface, OCCURRENCE)
        assert len(occurrence_rows(store)) == 1 and len(spawn.checked) == checks
        with pytest.raises(VerbError) as exc:  # repointing re-runs admission
            run(surface.recurring_update(message(
                "schedule.recurring.update", "hosta:requester", recurring_id=rid, spec_ids=[SPEC])))
        assert exc.value.code == "work_lane_assignment_unlinked"
    finally:
        store.stop()


# ---- authority --------------------------------------------------------------

def test_creator_sees_recurring_owned_rows_and_outsiders_do_not(tmp_path) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        force_next(store, rid, OCCURRENCE_ISO)
        pass_at(surface, OCCURRENCE)
        (row,) = occurrence_rows(store)
        mine = run(surface.schedule_list(message("schedule.list", "hosta:requester")))["schedules"]
        assert [item["schedule_id"] for item in mine] == [row["schedule_id"]]
        theirs = run(surface.schedule_list(message("schedule.list", "hosta:foreign")))["schedules"]
        assert theirs == []
        assert run(surface.recurring_list(message("schedule.recurring.list", "hosta:foreign")))["recurring"] == []
    finally:
        store.stop()


@pytest.mark.parametrize("verb", ["get", "update", "pause", "resume", "run", "delete"])
def test_outsider_seat_cannot_reach_another_seats_rule(tmp_path, verb) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        handler = getattr(surface, f"recurring_{verb}")
        extra = {"name": "stolen"} if verb == "update" else {}
        with pytest.raises(VerbError) as exc:
            run(handler(message(f"schedule.recurring.{verb}", "hosta:foreign", recurring_id=rid, **extra)))
        assert exc.value.code == "not_found"
        assert rule_row(store, rid)["name"] == "Nightly retro"
    finally:
        store.stop()


def test_scoped_or_unauthenticated_clients_are_refused(tmp_path) -> None:
    store, _s, _c, spawn, surface = harness(tmp_path)
    try:
        for ctx in ({}, {"token_verified": False}, {"scoped": True}):
            with pytest.raises(VerbError) as exc:
                run(surface.recurring_create({
                    "type": "schedule.recurring.create", "request_id": str(uuid.uuid4()),
                    "from_stream_id": "hosta:requester", "_auth_context": ctx,
                    "name": "x", "every": "daily", "at_time": "02:00", "spec_ids": [SPEC],
                    "initial_prompt": "p",
                }))
            assert exc.value.code == "stream_ownership_unverified"
        assert spawn.admissions == []
    finally:
        store.stop()


def test_create_replays_by_request_id_and_refuses_blob_prompts(tmp_path) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        payload = message(
            "schedule.recurring.create", "hosta:requester", name="n", every="daily", at_time="02:00",
            provider="codex", model="gpt-5.6-sol", effort="high", spec_ids=[SPEC], parent="none",
            initial_prompt="p",
        )
        first = run(surface.recurring_create(dict(payload)))
        again = run(surface.recurring_create(dict(payload)))
        assert first["recurring"]["recurring_id"] == again["recurring"]["recurring_id"]
        assert len(run(surface.recurring_inventory())) == 1
        with pytest.raises(VerbError) as exc:
            run(surface.recurring_create(message(
                "schedule.recurring.create", "hosta:requester", name="b", every="daily", at_time="02:00",
                spec_ids=[SPEC], parent="none", initial_prompt_blob_sha="a" * 64)))
        assert exc.value.code == "unsupported_configuration"
    finally:
        store.stop()


@pytest.mark.parametrize("extra, code", [
    ({"handoff": True}, "unsupported_configuration"),
    ({"resume_session_id": "abc"}, "resume_not_schedulable"),
    ({"confirm_model_change": True}, "handoff_only_flag"),
    ({"idempotency_key": "k"}, "idempotency_key_not_schedulable"),
])
def test_create_refuses_the_one_shot_only_spawn_flags(tmp_path, extra, code) -> None:
    store, _s, _c, spawn, surface = harness(tmp_path)
    try:
        with pytest.raises(VerbError) as exc:
            create_rule(surface, **extra)
        assert exc.value.code == code
        assert spawn.admissions == []
    finally:
        store.stop()


def test_failing_rules_do_not_count_as_evaluated_so_recover_terminates(tmp_path, monkeypatch) -> None:
    store, _s, _c, _spawn, surface = harness(tmp_path)
    try:
        rid = create_rule(surface)["recurring_id"]
        force_next(store, rid, iso(datetime.now(UTC) - timedelta(minutes=1)))

        def boom(*_args, **_kwargs):
            raise RuntimeError("tz data gone")

        monkeypatch.setattr(surface, "_materialise_tx", boom)
        assert run(surface.materialise_due_recurring()) == 0
        run(surface.recover())  # returns instead of re-running the failing pass forever
    finally:
        store.stop()
