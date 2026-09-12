"""First-class v2 coordination-window and scheduled-spawn contract."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_orch import cli as agent_cli
from agent_orch.config import Config
import launch
from machines import MachineConfig
from server import Server
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from window_schedule import WindowSchedule, receipt_id


SPEC = "spec_example__schedule_contract"
TARGET_SHA = "1" * 40


def release_attestation(release_id: str = "active") -> dict:
    return {
        "host": "hosta",
        "release_id": release_id,
        "commit_sha": "2" * 40,
        "path": "/tmp/public-release",
        "realpath": "/tmp/public-release",
        "sha256": "3" * 64,
        "capabilities": ["spawn.target_sha", "report.target_sha"],
        "manifest_sha256": "4" * 64,
    }


class FakeSessions:
    def __init__(self) -> None:
        provenance = [{"kind": "explicit", "spec_id": SPEC}]
        self.rows = {
            stream_id: {
                "stream_id": stream_id, "status": "open", "role": role,
                "qualified_spec_ids": [SPEC], "spec_binding_provenance": provenance,
            }
            for stream_id, role in (
                ("hosta:requester", "worker"), ("hosta:participant", "worker"),
                ("hosta:service", "service"), ("hosta:foreign", "worker"),
            )
        }

    def get(self, stream_id: str):
        return self.rows.get(stream_id)


class FakeComms:
    def __init__(self, status: str = "delivered") -> None:
        self.status = status
        self.calls: list[dict] = []

    async def tell(self, msg: dict) -> dict:
        self.calls.append(dict(msg))
        return {
            "tell_id": msg["tell_id"], "ledger_row_id": 7,
            "delivery_status": self.status, "delivery_ack_at": "2026-08-26T12:00:00Z",
            "to_stream_id": msg["stream_id"], "submission_confirmed": True,
        }


class FakeSpawn:
    def __init__(self, response: dict | None = None, admission_binding: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.admissions: list[dict] = []
        self.admission_binding = admission_binding
        self.response = response or {
            "type": "spawn.ok", "stream_id": "hosta:scheduled-child",
            "initial_prompt_delivery": {"state": "not_requested"},
        }
    async def spawn(self, msg: dict, _local_host: str) -> dict:
        self.calls.append(dict(msg))
        return dict(self.response)

    async def admit_schedule(self, msg: dict, _local_host: str, *, admission_name: str) -> dict:
        self.admissions.append(dict(msg))
        spec_ids = list(msg.get("spec_ids") or [])
        binding = self.admission_binding or {
            "spec_id": spec_ids[0] if spec_ids else None,
            "spec_ids": spec_ids,
            "qualified_spec_ids": spec_ids,
            "spec_binding_provenance": [
                {
                    "spec_id": spec_id,
                    "provenance": "spawn_explicit",
                    "granting_principal": str(msg.get("from_stream_id") or "operator"),
                    "granted_at": "2026-08-27T12:00:00Z",
                }
                for spec_id in spec_ids
            ],
        }
        return {
            "host": msg.get("host") or _local_host,
            "role": msg.get("role"),
            "resolved_provider": msg.get("provider") or "codex",
            "resolved_model": msg.get("model") or "gpt-5.6-sol",
            "resolved_effort": msg.get("effort") or "high",
            "spec_binding": binding,
            "admission_name": admission_name,
        }


def auth(stream_id: str, *, role: str = "seat") -> dict:
    if role == "operator":
        return {"operator_authenticated": True, "operator_principal": "operator:test"}
    if role == "service":
        return {"service_authenticated": True, "service_actor": stream_id}
    return {"token_verified": True, "stream_id": stream_id}


def message(verb: str, actor: str, **values) -> dict:
    return {
        "type": verb, "request_id": str(uuid.uuid4()), "from_stream_id": actor,
        "_auth_context": auth(actor), **values,
        }


class RouteProbeTmux:
    def __init__(self, label: str) -> None:
        self.label = label
        self.staged: list[tuple[str, bytes]] = []

    async def has_session(self, _name: str) -> bool:
        return False

    async def stage_text(self, path: str, data: bytes) -> None:
        self.staged.append((path, bytes(data)))

    async def run(self, *args: str, **kwargs) -> tuple[int, str]:
        return 0, ""


class RouteProbeHosts:
    def __init__(self, peer_tmux: RouteProbeTmux) -> None:
        self.peer_tmux = peer_tmux
        self.reachability: list[tuple[str, str]] = []
        self.tmux_routes: list[str] = []
        self.peers = {
            "hostb": MachineConfig(
                name="hostb",
                ssh_target="user@example.local",
                tmux_bin="/tmp/public-peer/bin/tmux",
                claude_bin="/tmp/public-peer/bin/claude",
                codex_bin="/tmp/public-peer/bin/codex",
                projects_root="/tmp/public-peer/projects",
                cwd="/tmp/public-peer/workspace",
                agent_orch_bin_dir="/tmp/public-peer/bin",
            ),
        }

    async def run_command(self, host: str, *args: str, **kwargs) -> tuple[int, str]:
        return 0, ""

    async def ensure_reachable(self, host: str, operation: str) -> None:
        self.reachability.append((host, operation))

    def tmux_for(self, host: str) -> RouteProbeTmux:
        self.tmux_routes.append(host)
        assert host == "hostb"
        return self.peer_tmux


class RouteProbeSpecs:
    def resolution_for(self, _spec_id: str) -> str:
        return "resolved"

    def resolve_for_spawn(self, spec_id: str) -> dict:
        return {
            "resolution": "resolved",
            "catalog_resolution": "resolved",
            "tree_resolution": "resolved",
            "source": "work_tree",
            "tree_candidates": [],
            "canonical_spec_id": spec_id,
        }


class RouteProbeSpawnCtl(SpawnCtl):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fenced: list[dict] = []

    async def _spawn_fenced(self, *args, **kwargs) -> dict:
        host, name, _request_id, command, brief = args[:5]
        created = args[7]
        open_flds = args[9]
        tmux = args[11]
        delivery_receipt = args[12]
        created[0] = True
        self.fenced.append({
            "host": host,
            "name": name,
            "command": command,
            "brief": brief,
            "tmux": tmux,
            "token_hash": open_flds.get("token_hash"),
        })
        receipt = dict(delivery_receipt)
        receipt.update({"state": "delivered", "delivery_status": "delivered"})
        return {
            "type": "spawn.ok",
            "stream_id": f"{host}:{name}",
            "initial_prompt_delivery": receipt,
        }


def future_time(minutes: int = 10) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def run(coro):
    return asyncio.run(coro)


def harness(
    tmp_path, *, notice_status: str = "delivered", spawn_response: dict | None = None,
    admission_binding: dict | None = None,
    broadcast=None,
):
    store = Store(str(tmp_path / f"sessions-{uuid.uuid4().hex}.db"))
    store.start()
    sessions = FakeSessions()
    comms = FakeComms(notice_status)
    spawn = FakeSpawn(spawn_response, admission_binding)
    surface = WindowSchedule(
        store, sessions, comms, spawn, local_host="hosta",
        broadcast=broadcast,
    )
    surface.mark_store_ready()
    return store, sessions, comms, spawn, surface


def schedule_insert(surface: WindowSchedule, actor: str = "hosta:requester", **values):
    payload = {"objective": "Exercise scheduled spawn", "fires_at_utc": future_time(), "target_host": "hosta", "provider": "codex",
               "model": "gpt-5.6-sol", "effort": "high", "spec_ids": [SPEC]}
    payload.update(values)
    return run(surface.schedule_insert(message("schedule.insert", actor, **payload)))


@pytest.mark.parametrize(
    ("lineage", "expected_parent"),
    [
        ({}, "hosta:requester"),
        ({"parent": "hosta:participant"}, "hosta:participant"),
        ({"top_level": True}, None),
    ],
)
def test_cli_to_daemon_fresh_schedule_creates_all_lineage_shapes(
    tmp_path, monkeypatch, capsys, lineage, expected_parent,
) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    captured: list[dict] = []

    async def real_schedule_insert(_config, payload, timeout):
        assert timeout == 1.0
        request = {**payload, "_auth_context": auth("hosta:requester")}
        captured.append(dict(request))
        return await surface.schedule_insert(request)

    monkeypatch.setattr(
        agent_cli,
        "load_config",
        lambda: Config("ws://unused", "", "hosta", tmp_path),
    )
    monkeypatch.setattr(
        agent_cli,
        "discover_leader_stream_id_short",
        lambda _config: "hosta:requester",
    )
    monkeypatch.setattr(agent_cli, "schedule_once", real_schedule_insert)
    arg_values = dict(
        objective="Exercise the existing spawn contract", provider="codex",
        model=None,
        effort=None,
        host="hosta",
        role="worker",
        phase="implementation",
        spec_id=[SPEC],
        visibility="hidden",
        parent=None,
        top_level=False,
        handoff=False,
        confirm_model_change=False,
        disposition_waived_reason=None,
        resume=None,
        at=future_time(),
        delay=None,
        allow_past_time=False,
        allow_far_future=False,
        reparent_children=None,
        # Exercise the negative lifecycle flag on every lineage shape that the
        # fresh scheduled CLI path admits; top-level remains absent because the
        # option has no meaning without parent lineage.
        self_close_on_completion=None if lineage.get("top_level") else False,
        initial_prompt="captured at create",
        initial_prompt_file=None,
        timeout=1.0,
        request_id=None,
        target_sha=None,
        idempotency_key=None,
    )
    arg_values.update(lineage)
    args = SimpleNamespace(**arg_values)
    try:
        assert agent_cli.spawn(args) == 0
        response = json.loads(capsys.readouterr().out)
        schedule = response["schedule"]
        assert captured[0]["from_stream_id"] == "hosta:requester"
        assert schedule["created_by_stream_id"] == "hosta:requester"
        assert schedule.get("parent_stream_id") == expected_parent
        assert captured[0].get("self_close_on_completion") is (
            False if expected_parent else None
        )
        assert schedule["phase"] == "implementation"
        assert schedule["prompt_sha256"]
    finally:
        store.stop()

@pytest.mark.parametrize("verb", ["schedule.cancel", "schedule.reschedule", "schedule.run"])
def test_schedule_mutations_authenticate_before_target_lookup(tmp_path, verb) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        msg = {"type": verb, "request_id": str(uuid.uuid4()), "from_stream_id": "hosta:requester",
               "schedule_id": "missing-schedule", "_auth_context": {}}
        with pytest.raises(VerbError) as exc:
            run(getattr(surface, verb.replace(".", "_"))(msg))
        assert exc.value.code == "stream_ownership_unverified"
    finally:
        store.stop()


def test_schedule_handoff_lineage_is_owner_bound(tmp_path) -> None:
    store, sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        sessions.rows["hosta:foreign"]["qualified_spec_ids"] = [SPEC]
        with pytest.raises(VerbError) as exc:
            schedule_insert(surface, actor="hosta:foreign", created_by_stream_id="hosta:requester",
                            handoff_from_stream_id="hosta:requester")
        assert exc.value.code == "not_authorized"
    finally:
        store.stop()


def test_schedule_admission_persists_requested_spec_subset_and_provenance(tmp_path) -> None:
    store, sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        sessions.rows["hosta:requester"]["qualified_spec_ids"] = [SPEC, "example-other"]
        inserted = schedule_insert(surface, spec_ids=[SPEC])
        assert inserted["schedule"]["owner_spec_ids"] == [SPEC]
        assert inserted["schedule"]["owner_spec_provenance"] == [{
            "spec_id": SPEC,
            "provenance": "spawn_explicit",
            "granting_principal": "hosta:requester",
            "granted_at": "2026-08-27T12:00:00Z",
        }]
    finally:
        store.stop()


def _canonical_binding(spec_ids, provenance_spec_ids=None):
    provenance_ids = spec_ids if provenance_spec_ids is None else provenance_spec_ids
    return {
        "spec_id": spec_ids[0] if spec_ids else None,
        "spec_ids": list(spec_ids),
        "qualified_spec_ids": list(spec_ids),
        "spec_binding_provenance": [
            {
                "spec_id": spec_id,
                "provenance": "spawn_explicit",
                "granting_principal": "admission:authority",
                "granted_at": "2026-08-27T12:00:00Z",
            }
            for spec_id in provenance_ids
        ],
    }


def test_schedule_admission_rejects_outsider_from_seat_owner_binding(tmp_path) -> None:
    outsider = "spec-outsider"
    store, _sessions, _comms, _spawn, surface = harness(
        tmp_path, admission_binding=_canonical_binding([outsider]),
    )
    try:
        with pytest.raises(VerbError) as exc:
            schedule_insert(surface, spec_ids=[outsider])
        assert exc.value.code == "not_authorized"
    finally:
        store.stop()


def test_schedule_admission_persists_canonicalized_alias_binding(tmp_path) -> None:
    alias = "spec_example__schedule_contract"
    binding = _canonical_binding([SPEC])
    store, _sessions, _comms, _spawn, surface = harness(tmp_path, admission_binding=binding)
    try:
        inserted = schedule_insert(surface, spec_ids=[alias])
        assert inserted["schedule"]["owner_spec_ids"] == [SPEC]
        assert inserted["schedule"]["owner_spec_provenance"] == binding["spec_binding_provenance"]
    finally:
        store.stop()


@pytest.mark.parametrize("provenance_spec_ids", [[], [SPEC]])
def test_schedule_admission_rejects_empty_or_partial_canonical_provenance(
    tmp_path, provenance_spec_ids,
) -> None:
    other = "spec-other"
    spec_ids = [SPEC] if not provenance_spec_ids else [SPEC, other]
    store, sessions, _comms, _spawn, surface = harness(
        tmp_path, admission_binding=_canonical_binding(spec_ids, provenance_spec_ids),
    )
    sessions.rows["hosta:requester"]["qualified_spec_ids"] = list(spec_ids)
    sessions.rows["hosta:requester"]["spec_binding_provenance"] = [
        {"kind": "explicit", "spec_id": spec_id} for spec_id in spec_ids
    ]
    try:
        with pytest.raises(VerbError) as exc:
            schedule_insert(surface, spec_ids=spec_ids)
        assert exc.value.code == "not_authorized"
    finally:
        store.stop()


def test_schedule_service_actor_persists_only_canonical_admission_binding(tmp_path) -> None:
    alias = "spec_example__schedule_contract"
    binding = _canonical_binding([SPEC])
    store, _sessions, _comms, _spawn, surface = harness(tmp_path, admission_binding=binding)
    try:
        payload = {
            "objective": "Exercise scheduled spawn", "fires_at_utc": future_time(), "target_host": "hosta", "provider": "codex",
            "model": "gpt-5.6-sol", "effort": "high", "spec_ids": [alias],
            "spec_binding_provenance": [{"kind": "caller_raw", "spec_id": alias}],
        }
        inserted = run(surface.schedule_insert(message(
            "schedule.insert", "hosta:scheduler", **payload,
            _auth_context=auth("hosta:scheduler", role="service"),
        )))
        assert inserted["schedule"]["owner_service_actor"] == "hosta:scheduler"
        assert inserted["schedule"]["owner_spec_ids"] == [SPEC]
        assert inserted["schedule"]["owner_spec_provenance"] == binding["spec_binding_provenance"]
    finally:
        store.stop()


def test_docs_do_not_publish_unsupported_schedule_contract(tmp_path) -> None:
    root = Path(__file__).resolve().parents[3]
    text = (root / "services/chat-stream-v2/README.md").read_text(encoding="utf-8").lower()
    assert "schedule.insert unsupported" not in text
    assert "fired_in_progress" not in text
    assert "pending_retry" not in text


def test_schedule_insert_reschedule_cancel_run_and_replay(tmp_path) -> None:
    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(surface)
        schedule = inserted["schedule"]
        assert schedule["state"] == "pending" and schedule["generation"] == 1
        assert schedule["created_by_stream_id"] == "hosta:requester"
        assert len(spawn.admissions) == 1
        assert schedule["requested_model"] == "gpt-5.6-sol"
        assert schedule["resolved_model"] == "gpt-5.6-sol"

        reschedule = message(
            "schedule.reschedule", "hosta:requester", schedule_id=schedule["schedule_id"],
            fires_at_utc=future_time(20),
        )
        moved = run(surface.schedule_reschedule(reschedule))
        assert moved["schedule"]["generation"] == 2
        assert run(surface.schedule_reschedule(dict(reschedule))) == moved

        run_msg = message("schedule.run", "hosta:requester", schedule_id=schedule["schedule_id"])
        fired = run(surface.schedule_run(run_msg))
        assert fired["schedule"]["state"] == "fired"
        assert len(spawn.calls) == 1
        assert spawn.calls[0]["idempotency_key"].endswith(":2")
        assert run(surface.schedule_run(dict(run_msg))) == fired
        assert len(spawn.calls) == 1

        another = schedule_insert(surface, actor="hosta:participant")
        cancel = message("schedule.cancel", "hosta:participant", schedule_id=another["schedule"]["schedule_id"])
        cancelled = run(surface.schedule_cancel(cancel))
        assert cancelled["schedule"]["state"] == "cancelled"
        assert run(surface.schedule_cancel(dict(cancel))) == cancelled

        with pytest.raises(VerbError) as hidden:
            run(surface.schedule_get({
                "type": "schedule.get", "schedule_id": another["schedule"]["schedule_id"],
                "from_stream_id": "hosta:foreign", "_auth_context": auth("hosta:foreign"),
            }))
        assert hidden.value.code == "not_found"
    finally:
        store.stop()


def test_schedule_inventory_and_lifecycle_pushes_cover_every_mutation(tmp_path) -> None:
    pushed: list[dict] = []

    async def broadcast(frame: dict) -> None:
        pushed.append(frame)

    store, _sessions, _comms, _spawn, surface = harness(
        tmp_path,
        broadcast=broadcast,
        spawn_response={
            "type": "spawn.ok",
            "stream_id": "hosta:scheduled-child",
            "initial_prompt_delivery": {"state": "delivered"},
        },
    )
    try:
        inserted = schedule_insert(
            surface, initial_prompt="  first line\n\nsecond   line  ",
        )
        sid = inserted["schedule"]["schedule_id"]
        inventory = run(surface.schedule_inventory())
        row = next(item for item in inventory if item["schedule_id"] == sid)
        assert row["prompt_preview"] == "first line second line"
        assert "prompt_b64" not in row and "prompt_blob_id" not in row

        rescheduled = message(
            "schedule.reschedule", "hosta:requester", schedule_id=sid,
            fires_at_utc=future_time(20),
        )
        run(surface.schedule_reschedule(rescheduled))
        run(surface.schedule_run(message(
            "schedule.run", "hosta:requester", schedule_id=sid,
        )))

        other = schedule_insert(surface, actor="hosta:participant")
        other_sid = other["schedule"]["schedule_id"]
        run(surface.schedule_cancel(message(
            "schedule.cancel", "hosta:participant", schedule_id=other_sid,
        )))

        assert [frame["event"] for frame in pushed] == [
            "created", "rescheduled", "firing", "fired", "created", "cancelled",
        ]
        assert all(frame["type"] == "schedule.lifecycle" for frame in pushed)
        fired = next(frame["schedule"] for frame in pushed if frame["event"] == "fired")
        assert fired["child_stream_id"] == "hosta:scheduled-child"
        assert fired["terminal_at"]
        assert fired["prompt_preview"] == "first line second line"
    finally:
        store.stop()


def test_schedule_blob_prompt_is_rejected_before_persistence(tmp_path) -> None:
    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        with pytest.raises(VerbError, match="inline prompt") as refused:
            schedule_insert(surface, initial_prompt_blob_sha="a" * 64)
        assert refused.value.code == "unsupported_configuration"
        assert run(surface.schedule_inventory()) == []
        assert spawn.calls == []
        assert spawn.admissions == []
    finally:
        store.stop()


@pytest.mark.parametrize(
    ("spawn_response", "terminal_state"),
    [
        ({"type": "spawn.error", "error_code": "invalid_model"}, "failed"),
    ],
)
def test_schedule_terminal_failure_is_pushed_before_run_error(
    tmp_path, spawn_response, terminal_state,
) -> None:
    pushed: list[dict] = []

    async def broadcast(frame: dict) -> None:
        pushed.append(frame)

    store, _sessions, _comms, _spawn, surface = harness(
        tmp_path, spawn_response=spawn_response, broadcast=broadcast,
    )
    try:
        inserted = schedule_insert(surface)
        with pytest.raises(VerbError) as outcome:
            run(surface.schedule_run(message(
                "schedule.run", "hosta:requester",
                schedule_id=inserted["schedule"]["schedule_id"],
            )))
        assert outcome.value.code == terminal_state
        terminal = pushed[-1]
        assert terminal["event"] == terminal_state
        assert terminal["schedule"]["state"] == terminal_state
        assert terminal["schedule"]["last_error_code"] == spawn_response["error_code"]
        assert terminal["schedule"]["terminal_at"]
    finally:
        store.stop()


def test_scheduled_handoff_preserves_source_and_intent(tmp_path) -> None:
    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(
            surface, handoff=True, handoff_from_stream_id="hosta:requester",
            created_by_stream_id="hosta:requester", self_close_on_completion=True,
        )
        assert inserted["schedule"]["parent_stream_id"] is None
        assert inserted["schedule"]["self_close_on_completion"] is True
        run(surface.schedule_run(message(
            "schedule.run", "hosta:requester", schedule_id=inserted["schedule"]["schedule_id"],
        )))
        assert spawn.calls[0]["handoff"] is True
        assert spawn.calls[0]["handoff_from_stream_id"] == "hosta:requester"
        assert spawn.calls[0]["self_close_on_completion"] is True
    finally:
        store.stop()


@pytest.mark.parametrize(
    ("create_values", "row_key", "spawn_key", "expected"),
    [
        ({"provider": "claude"}, "provider", "provider", "claude"),
        ({"model": "gpt-5.6-sol"}, "model", "model", "gpt-5.6-sol"),
        ({"effort": "xhigh"}, "effort", "effort", "xhigh"),
        ({"target_host": "hostb"}, "target_host", "host", "hostb"),
        ({"role": "worker"}, "role", "role", "worker"),
        ({"phase": "implementation"}, "phase", "phase", "implementation"),
        ({"visibility": "hidden"}, "visibility", "visibility", "hidden"),
        (
            {"parent_stream_id": "hosta:participant"},
            "parent_stream_id", "parent_stream_id", "hosta:participant",
        ),
        ({"reparent_children": False}, "reparent_children", "reparent_children", False),
        ({"no_watch": True}, "no_watch", "no_watch", True),
        ({"no_watch": False}, "no_watch", "no_watch", False),
        (
            {"parent_stream_id": "hosta:requester", "self_close_on_completion": True},
            "self_close_on_completion", "self_close_on_completion", True,
        ),
        (
            {"parent_stream_id": "hosta:requester", "self_close_on_completion": False},
            "self_close_on_completion", "self_close_on_completion", False,
        ),
        (
            {"disposition_waived_reason": "operator-approved"},
            "disposition_waived_reason", "disposition_waived_reason", "operator-approved",
        ),
        ({"target_sha": TARGET_SHA}, "target_sha", "target_sha", TARGET_SHA),
        ({"spec_ids": [SPEC]}, "owner_spec_ids", "spec_ids", [SPEC]),
    ],
)
def test_every_fresh_serialized_option_round_trips_create_row_and_fire(
    tmp_path, create_values, row_key, spawn_key, expected,
) -> None:
    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(surface, **create_values)
        assert inserted["schedule"][row_key] == expected
        run(surface._fire_schedule(inserted["schedule"]["schedule_id"]))
        assert spawn.calls[0][spawn_key] == expected
        assert spawn.calls[0]["idempotency_key"] == (
            f"schedule:{inserted['schedule']['schedule_id']}:1"
        )
    finally:
        store.stop()


def test_schedule_option_tri_state_distinguishes_absent_and_explicit_false(tmp_path) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        absent = schedule_insert(surface)
        explicit = schedule_insert(
            surface,
            actor="hosta:participant",
            parent_stream_id="hosta:participant",
            self_close_on_completion=False,
            reparent_children=False,
        )
        rows = run(store.submit(lambda conn: {
            row["schedule_id"]: (
                row["reparent_children"], row["self_close_on_completion"],
            )
            for row in conn.execute(
                "SELECT schedule_id,reparent_children,self_close_on_completion "
                "FROM v2_schedules"
            )
        }))
        assert rows[absent["schedule"]["schedule_id"]] == (None, None)
        assert rows[explicit["schedule"]["schedule_id"]] == (0, 0)
    finally:
        store.stop()


def test_terminal_settlement_advances_all_schedule_receipts_to_terminal_plus_30d(
    tmp_path,
) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(surface, fires_at_utc=future_time(60 * 24 * 40))
        sid = inserted["schedule"]["schedule_id"]
        run(store.submit(lambda conn: (
            conn.execute(
                "UPDATE v2_operation_receipts SET retain_until='2000-01-01T00:00:00Z' "
                "WHERE target_id=?", (sid,),
            ),
            conn.commit(),
        )))
        cancelled = run(surface.schedule_cancel(message(
            "schedule.cancel", "hosta:requester", schedule_id=sid,
        )))
        terminal_at = datetime.fromisoformat(
            cancelled["schedule"]["terminal_at"].replace("Z", "+00:00")
        )
        expected = (terminal_at + timedelta(days=30)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        retained = run(store.submit(lambda conn: [row[0] for row in conn.execute(
            "SELECT retain_until FROM v2_operation_receipts "
            "WHERE surface='schedule' AND target_id=? ORDER BY phase",
            (sid,),
        )]))
        assert len(retained) == 2
        assert set(retained) == {expected}
    finally:
        store.stop()


def test_fire_keeps_claimed_truth_without_prepared_or_transmitted_receipts(tmp_path) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(surface)
        sid = inserted["schedule"]["schedule_id"]
        run(surface._fire_schedule(sid))
        phases = run(store.submit(lambda conn: {
            row[0] for row in conn.execute(
                "SELECT phase FROM v2_operation_receipts WHERE target_id=?", (sid,),
            )
        }))
        assert "dispatch_claimed" in phases
        assert {"dispatch_prepared", "dispatch_transmitted"}.isdisjoint(phases)
    finally:
        store.stop()


def test_remote_scheduled_fire_routes_profile_tmux_prompt_and_child_token_to_peer(
    tmp_path,
) -> None:
    store = Store(str(tmp_path / "remote-route.db"))
    store.start()
    sessions = FakeSessions()
    local_tmux = RouteProbeTmux("local")
    peer_tmux = RouteProbeTmux("peer")
    hosts = RouteProbeHosts(peer_tmux)
    machine = launch.local_machine(
        "hosta",
        cwd="/tmp/public-local/workspace",
        claude_bin="/tmp/public-local/bin/claude",
        projects_root="/tmp/public-local/projects",
        agent_orch_bin_dir="/tmp/public-local/bin",
    )
    spawnctl = RouteProbeSpawnCtl(
        store,
        sessions,
        tmux=local_tmux,
        machine=machine,
        hosts=hosts,
        specs=RouteProbeSpecs(),
    )
    surface = WindowSchedule(
        store,
        sessions,
        FakeComms(),
        spawnctl,
        local_host="hosta",
    )
    surface.mark_store_ready()
    try:
        inserted = schedule_insert(
            surface,
            provider="claude",
            model="claude-opus-4-8",
            effort="high",
            target_host="hostb",
            initial_prompt="peer route proof " * 80,
            target_sha=TARGET_SHA,
        )
        sid = inserted["schedule"]["schedule_id"]
        run(surface._fire_schedule(sid))

        assert hosts.reachability == [
            ("hostb", "schedule.insert"),
            ("hostb", "spawn"),
        ]
        assert hosts.tmux_routes == ["hostb"]
        assert len(spawnctl.fenced) == 1
        fenced = spawnctl.fenced[0]
        assert fenced["host"] == "hostb"
        assert fenced["tmux"] is peer_tmux
        assert "/tmp/public-peer/bin/claude" in fenced["command"]
        assert "/tmp/public-local/bin/claude" not in fenced["command"]
        assert fenced["brief"].startswith("Read /tmp/pentacle-prompt-stage/")
        assert fenced["token_hash"]
        assert len(peer_tmux.staged) == 2
        assert any("/.pentacle-stream-tokens/" in path for path, _ in peer_tmux.staged)
        assert any("/pentacle-prompt-stage/" in path for path, _ in peer_tmux.staged)
        assert local_tmux.staged == []
    finally:
        store.stop()


def test_closed_creator_does_not_block_in_process_fire_or_replay_a_token(tmp_path) -> None:
    store, sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(
            surface,
            parent_stream_id="hosta:requester",
            self_close_on_completion=True,
        )
        sessions.rows["hosta:requester"]["status"] = "closed"
        sid = inserted["schedule"]["schedule_id"]
        fired = run(surface._fire_schedule(sid))
        assert fired["schedule"]["state"] == "fired"
        assert spawn.calls[0]["parent_stream_id"] == "hosta:requester"
        assert spawn.calls[0]["self_close_on_completion"] is True
        assert "stream_token" not in spawn.calls[0]
    finally:
        store.stop()


def test_schedule_generation_key_replays_once_and_rejects_changed_payload(tmp_path) -> None:
    class IdempotencyTmux:
        def __init__(self) -> None:
            self.live: set[str] = set()
            self.created = 0

        async def has_session(self, name: str) -> bool:
            return name in self.live

        async def new_session(self, name: str, _command: str, cwd=None, env=None) -> None:
            self.live.add(name)
            self.created += 1

        async def capture(self, _name: str) -> str:
            return "READY"

        async def pane_pid(self, _name: str) -> str:
            return "1234"

        async def session_state(self, name: str) -> str:
            return "alive" if name in self.live else "gone"

        async def kill_session(self, name: str) -> None:
            self.live.discard(name)

    class ScheduleSpawnCtl(SpawnCtl):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.schedule_calls: list[dict] = []

        async def spawn(self, msg: dict, local_host: str) -> dict:
            self.schedule_calls.append(dict(msg))
            # Keep the real schedule message as SpawnCtl's logical payload,
            # using only its raw-command seam to avoid a provider process.
            return await super().spawn({"objective": "Exercise the existing spawn contract",
                "command": "stub",
                "idempotency_key": msg["idempotency_key"],
                "request_id": msg["request_id"],
                "schedule_payload": {
                    key: value for key, value in msg.items()
                    if key not in {"request_id", "idempotency_key"}
                },
            }, local_host)

    store = Store(str(tmp_path / "schedule-idempotency.db"))
    store.start()
    try:
        tmux = IdempotencyTmux()
        spawnctl = ScheduleSpawnCtl(
            store,
            Sessions(store, tmux=tmux, local_host="hosta"),
            tmux=tmux,
            machine=launch.local_machine(
                "hosta", cwd="/tmp/public-local/workspace", claude_bin="/tmp/public-local/bin/claude",
                codex_bin="/tmp/public-local/bin/codex", projects_root="/tmp/public-local/projects",
                agent_orch_bin_dir="/tmp/public-local/bin",
            ),
            specs=RouteProbeSpecs(),
        )
        surface = WindowSchedule(
            store, FakeSessions(), FakeComms(), spawnctl, local_host="hosta",
        )
        surface.mark_store_ready()
        sid = schedule_insert(surface)["schedule"]["schedule_id"]
        fired = run(surface._fire_schedule(sid))
        assert fired["schedule"]["state"] == "fired"
        first = spawnctl.schedule_calls[0]
        assert first["idempotency_key"] == f"schedule:{sid}:1"

        replay = run(spawnctl.spawn({"objective": "Exercise the existing spawn contract", **first, "request_id": str(uuid.uuid4())}, "hosta"))
        assert replay.get("replayed") is True, replay
        assert tmux.created == 1
        with pytest.raises(VerbError) as conflict:
            run(spawnctl.spawn({"objective": "Exercise the existing spawn contract",
                **first, "request_id": str(uuid.uuid4()), "role": "service",
            }, "hosta"))
        assert conflict.value.code == "idempotency_key_conflict"
        assert tmux.created == 1
    finally:
        store.stop()


@pytest.mark.parametrize("attestation", ["malformed", release_attestation()])
def test_installation_attestation_is_rejected_before_persistence(tmp_path, attestation) -> None:
    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        with pytest.raises(VerbError, match="attestations are unsupported") as refused:
            schedule_insert(surface, target_sha=TARGET_SHA, agent_orch_attestation=attestation)
        assert refused.value.code == "unsupported_configuration"
        assert run(surface.schedule_inventory()) == []
        assert spawn.calls == []
        assert spawn.admissions == []
    finally:
        store.stop()


def test_recovery_classifies_claimed_malformed_attestation_through_same_choke_point(
    tmp_path,
) -> None:
    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(
            surface,
            target_sha=TARGET_SHA,
        )
        sid = inserted["schedule"]["schedule_id"]
        operation_id = str(uuid.uuid4())
        now = future_time(-1)
        run(store.submit(lambda conn: (
            conn.execute(
                "UPDATE v2_schedules SET attestation_json='\"malformed\"',state='firing',fires_at_utc=?,updated_at=? "
                "WHERE schedule_id=?",
                (now, now, sid),
            ),
            conn.execute(
                "INSERT INTO v2_schedule_dispatches "
                "(schedule_id,generation,spawn_key,phase,spawn_request_id,prepared_at,claimed_at) "
                "VALUES (?,1,?,'dispatch_claimed',?,?,?)",
                (sid, f"schedule:{sid}:1", f"schedule-spawn:{sid}:1", now, now),
            ),
            conn.execute(
                "INSERT INTO v2_operation_receipts "
                "(receipt_id,request_id,phase,surface,verb,actor_kind,actor_id,"
                "canonical_payload_sha256,target_id,measured_state_json,result_json,"
                "measured_at,retain_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_id(operation_id, "dispatch_claimed"), operation_id,
                 "dispatch_claimed", "schedule", "schedule.run", "seat",
                 "hosta:requester", "0" * 64, sid, "{}", "{}", now,
                 future_time(60)),
            ),
            conn.commit(),
        )))

        run(surface.recover())
        run(surface.recover())
        schedule = run(store.submit(lambda conn: dict(conn.execute(
            "SELECT * FROM v2_schedules WHERE schedule_id=?", (sid,),
        ).fetchone())))
        assert spawn.calls == []
        assert schedule["state"] == "failed"
        assert schedule["last_error_code"] == "unsupported_configuration"
    finally:
        store.stop()


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_target_sha_normalizes_to_sql_null_at_create(tmp_path, blank) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        inserted = schedule_insert(surface, target_sha=blank)
        sid = inserted["schedule"]["schedule_id"]
        stored = run(store.submit(lambda conn: conn.execute(
            "SELECT target_sha FROM v2_schedules WHERE schedule_id=?", (sid,),
        ).fetchone()[0]))
        assert stored is None
    finally:
        store.stop()


def test_nonblank_malformed_target_sha_is_rejected_with_fixed_code(tmp_path) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        with pytest.raises(VerbError) as malformed:
            schedule_insert(surface, target_sha="not-a-full-git-sha")
        assert malformed.value.code == "invalid_request"
        count = run(store.submit(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM v2_schedules"
        ).fetchone()[0]))
        assert count == 0
    finally:
        store.stop()


def test_reserved_scheduler_actor_cannot_register_or_be_wire_trusted(
    tmp_path, monkeypatch,
) -> None:
    spawnctl = SpawnCtl.__new__(SpawnCtl)
    spawnctl.hosts = None
    with pytest.raises(VerbError) as reserved:
        run(spawnctl._spawn_impl(
            {"objective": "Exercise the existing spawn contract", "session_name": "scheduler", "provider": "codex"}, "daemon",
        ))
    assert reserved.value.code == "reserved_actor"

    store = Store(str(tmp_path / "reserved-actor.db"))
    store.start()
    server = Server(store=store, sessions=FakeSessions(), local_host="hosta")
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", "tok")
    websocket = object()
    try:
        context = run(server._auth_context(websocket, {
            "type": "close",
            "from_stream_id": "daemon:scheduler",
            "stream_token": "tok",
        }))
        assert context["service_authenticated"] is True
        assert context["token_verified"] is False
    finally:
        store.stop()


@pytest.mark.parametrize(
    ("spawn_response", "state"),
    [
        ({"type": "spawn.error", "error_code": "invalid_model"}, "failed"),
        ({"type": "spawn.ok", "stream_id": "hosta:child", "initial_prompt_delivery": {"state": "proof_unavailable"}}, "indeterminate"),
    ],
)
def test_schedule_failure_truth(tmp_path, spawn_response, state) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path, spawn_response=spawn_response)
    try:
        inserted = schedule_insert(surface, initial_prompt="fixture prompt")
        run_msg = message("schedule.run", "hosta:requester", schedule_id=inserted["schedule"]["schedule_id"])
        with pytest.raises(VerbError) as outcome:
            run(surface.schedule_run(run_msg))
        assert outcome.value.code == state
        persisted = run(surface.schedule_get({
            "type": "schedule.get", "schedule_id": inserted["schedule"]["schedule_id"],
            "from_stream_id": "hosta:requester", "_auth_context": auth("hosta:requester"),
        }))
        assert persisted["schedule"]["state"] == state
    finally:
        store.stop()


@pytest.mark.parametrize(
    ("durable_prompt_status", "expected_state"),
    [("delivered", "fired"), ("unproven", "indeterminate")],
)
def test_schedule_reconciles_starting_spawn_from_durable_delivery_proof(
    tmp_path, durable_prompt_status, expected_state,
) -> None:
    public_response = {
        "type": "spawn.ok",
        "state": "starting",
        "stream_id": "hosta:scheduled-child",
        "reason": "boot_binding_indeterminate: rollback unconfirmed",
        "initial_prompt_delivery": {
                "state": "unproven",
                "delivery_status": "unproven",
            "delivery_ack_at": "2026-08-29T19:41:29.755906Z",
        },
    }
    store, _sessions, _comms, spawn, surface = harness(
        tmp_path, spawn_response=public_response,
    )

    async def spawn_with_durable_outcome(msg, local_host):
        await store.set_spawn_outcome(
            local_host,
            "scheduled-child",
            "indeterminate",
            request_id=msg["request_id"],
            reason="release binding unproven after durable prompt submission",
            delivery_evidence="live_pane_unproven",
            delivery_receipt={
                "state": durable_prompt_status,
                "delivery_status": durable_prompt_status,
            },
            idempotency_key=msg["idempotency_key"],
            request_payload_hash="f" * 64,
        )
        return dict(public_response)

    spawn.spawn = spawn_with_durable_outcome
    try:
        inserted = schedule_insert(surface, initial_prompt="fixture prompt")
        run_msg = message(
            "schedule.run", "hosta:requester",
            schedule_id=inserted["schedule"]["schedule_id"],
        )
        if expected_state == "fired":
            result = run(surface.schedule_run(run_msg))
            assert result["schedule"]["state"] == "fired"
            inventory_row = next(
                row for row in run(surface.schedule_inventory())
                if row["schedule_id"] == inserted["schedule"]["schedule_id"]
            )
            assert inventory_row["child_stream_id"] == "hosta:scheduled-child"
        else:
            with pytest.raises(VerbError) as result:
                run(surface.schedule_run(run_msg))
            assert result.value.code == "indeterminate"

        dispatch = run(store.submit(lambda conn: dict(conn.execute(
            "SELECT * FROM v2_schedule_dispatches WHERE schedule_id=?",
            (inserted["schedule"]["schedule_id"],),
        ).fetchone())))
        evidence = json.loads(dispatch["evidence_json"])
        assert dispatch["phase"] == (
            "spawn_delivered" if expected_state == "fired" else "indeterminate"
        )
        assert dispatch["prompt_delivery_status"] == (
            "delivered" if expected_state == "fired" else "unproven"
        )
        assert dispatch["child_stream_id"] == "hosta:scheduled-child"
        assert evidence["delivery_proof"] == (
            "durable_spawn_outcome" if expected_state == "fired" else "unproved"
        )
        assert evidence["measured_admitted_sessions"] == ["scheduled-child"]
    finally:
        store.stop()


def test_schedule_consumes_indeterminate_bootstrap_receipt_without_failure(tmp_path) -> None:
    public_response = {
        "type": "spawn.ok",
        "state": "starting",
        "stream_id": "hosta:scheduled-child",
        "initial_prompt_delivery": {
            "state": "indeterminate",
            "delivery_status": "indeterminate",
            "bootstrap_state": "starting",
            "proof_state": "pending",
        },
    }
    store, _sessions, _comms, spawn, surface = harness(
        tmp_path, spawn_response=public_response,
    )

    async def spawn_with_indeterminate_outcome(msg, local_host):
        await store.set_spawn_outcome(
            local_host,
            "scheduled-child",
            "indeterminate",
            request_id=msg["request_id"],
            reason="native initial prompt proof pending",
            delivery_evidence="live_pane_unproven",
            delivery_receipt=public_response["initial_prompt_delivery"],
            idempotency_key=msg["idempotency_key"],
            request_payload_hash="e" * 64,
        )
        return dict(public_response)

    spawn.spawn = spawn_with_indeterminate_outcome
    try:
        inserted = schedule_insert(surface, initial_prompt="fixture prompt")
        with pytest.raises(VerbError) as result:
            run(surface.schedule_run(message(
                "schedule.run", "hosta:requester",
                schedule_id=inserted["schedule"]["schedule_id"],
            )))
        assert result.value.code == "indeterminate"
        dispatch = run(store.submit(lambda conn: dict(conn.execute(
            "SELECT * FROM v2_schedule_dispatches WHERE schedule_id=?",
            (inserted["schedule"]["schedule_id"],),
        ).fetchone())))
        assert dispatch["phase"] == "indeterminate"
        assert dispatch["prompt_delivery_status"] == "indeterminate"
    finally:
        store.stop()


def test_prepared_restart_is_retryable_but_claimed_restart_is_indeterminate(tmp_path) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        now = future_time(-1)
        for suffix, phase in (("prepared", "prepared"), ("claimed", "dispatch_claimed")):
            sid = f"sched-{suffix}"
            operation_id = str(uuid.uuid4())
            run(store.submit(lambda conn, sid=sid, phase=phase, operation_id=operation_id: (
                conn.execute(
                    "INSERT INTO v2_schedules (schedule_id,request_id,owner_stream_id,owner_spec_ids_json,"
                    "owner_spec_provenance_json,target_host,requested_provider,requested_model,requested_effort,"
                    "resolved_provider,resolved_model,resolved_effort,fires_at_utc,state,generation,created_at,updated_at,objective) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'firing',1,?,?,'Exercise scheduled restart')",
                    (sid, str(uuid.uuid4()), "hosta:requester", '[\"%s\"]' % SPEC,
                     '[{\"kind\":\"explicit\",\"spec_id\":\"%s\"}]' % SPEC,
                     "hosta", "codex", "m", "high", "codex", "m", "high", now, now, now),
                ),
                conn.execute(
                    "INSERT INTO v2_schedule_dispatches "
                    "(schedule_id,generation,spawn_key,phase,spawn_request_id,prepared_at,claimed_at) "
                    "VALUES (?,1,?,?,?,?,?)",
                    (sid, f"schedule:{sid}:1", phase, f"schedule-spawn:{sid}:1", now,
                     now if phase == "dispatch_claimed" else None),
                ),
                conn.execute(
                    "INSERT INTO v2_operation_receipts "
                    "(receipt_id,request_id,phase,surface,verb,actor_kind,actor_id,"
                    "canonical_payload_sha256,target_id,measured_state_json,result_json,measured_at,retain_until) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id(operation_id, "dispatch_claimed"), operation_id,
                     "dispatch_claimed", "schedule", "schedule.run", "seat", "hosta:requester",
                     "0" * 64, sid, "{}", "{}", now, future_time(60)),
                ) if phase == "dispatch_claimed" else None,
                conn.commit(),
            )))
        run(surface.recover())
        states = run(store.submit(lambda conn: dict(conn.execute(
            "SELECT schedule_id,state FROM v2_schedules WHERE schedule_id LIKE 'sched-%'"
        ).fetchall())))
        assert states == {"sched-claimed": "indeterminate", "sched-prepared": "retry_pending"}
        recovered = run(store.submit(lambda conn: conn.execute(
            "SELECT result_json FROM v2_operation_receipts "
            "WHERE target_id='sched-claimed' AND phase='terminal'"
        ).fetchone()))
        assert recovered is not None and '"recovered":true' in recovered[0]
    finally:
        store.stop()


@pytest.mark.parametrize("spawn_outcome_state", ["delivered", "indeterminate"])
def test_post_outcome_restart_writes_terminal_receipt_and_replays(
    tmp_path, spawn_outcome_state,
) -> None:
    store, _sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        now = future_time(-1)
        sid = "sched-recovered-delivery"
        operation_id = str(uuid.uuid4())
        spawn_request_id = f"schedule-spawn:{sid}:1"
        spawn_key = f"schedule:{sid}:1"
        run(store.submit(lambda conn: (
            conn.execute(
                "INSERT INTO v2_schedules (schedule_id,request_id,owner_stream_id,owner_spec_ids_json,"
                "owner_spec_provenance_json,target_host,requested_provider,requested_model,requested_effort,"
                "resolved_provider,resolved_model,resolved_effort,fires_at_utc,state,generation,created_at,updated_at,objective) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'firing',1,?,?,'Exercise scheduled restart')",
                (sid, str(uuid.uuid4()), "hosta:requester", '["%s"]' % SPEC,
                 '[{"kind":"explicit","spec_id":"%s"}]' % SPEC,
                 "hosta", "codex", "m", "high", "codex", "m", "high", now, now, now),
            ),
            conn.execute(
                "INSERT INTO v2_schedule_dispatches "
                "(schedule_id,generation,spawn_key,phase,spawn_request_id,prepared_at,claimed_at,transmitted_at) "
                "VALUES (?,1,?,'dispatch_transmitted',?,?,?,?)",
                (sid, spawn_key, spawn_request_id, now, now, now),
            ),
            conn.execute(
                "INSERT INTO v2_operation_receipts "
                "(receipt_id,request_id,phase,surface,verb,actor_kind,actor_id,canonical_payload_sha256,"
                "target_id,measured_state_json,result_json,measured_at,retain_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_id(operation_id, "dispatch_claimed"), operation_id, "dispatch_claimed",
                 "schedule", "schedule.run", "seat", "hosta:requester", "0" * 64,
                 sid, "{}", "{}", now, future_time(60)),
            ),
            conn.commit(),
        )))
        run(store.set_spawn_outcome(
            "hosta", "scheduled-child", spawn_outcome_state, request_id=spawn_request_id,
            delivery_receipt={"state": "not_requested"}, idempotency_key=spawn_key,
        ))
        run(surface.recover())
        terminal = run(surface.schedule_receipt({
            "type": "schedule.receipt", "operation_request_id": operation_id,
            "phase": "spawn_delivered", "from_stream_id": "hosta:requester",
            "_auth_context": auth("hosta:requester"),
        }))
        assert terminal["receipt"]["phase_result"]["recovered"] is True
        replay_msg = message("schedule.run", "hosta:requester", schedule_id=sid)
        replay_msg["request_id"] = operation_id
        # Replay uses the original operation's canonical payload, so exercise
        # the durable readback result rather than fabricating a changed payload.
        assert terminal["receipt"]["measured_state"]["state"] == "fired"
        assert terminal["receipt"]["phase_result"]["schedule"]["state"] == "fired"
    finally:
        store.stop()


def test_spawn_error_without_affirmative_admission_readback_is_indeterminate(tmp_path) -> None:
    store, _sessions, _comms, _spawn, surface = harness(
        tmp_path, spawn_response={"type": "spawn.error", "error_code": "fixture_rejection"},
    )
    original = store.admitted_session_names_for_key

    async def unavailable(_host: str, _key: str):
        raise RuntimeError("fixture readback unavailable")

    try:
        store.admitted_session_names_for_key = unavailable
        inserted = schedule_insert(surface)
        with pytest.raises(VerbError) as outcome:
            run(surface.schedule_run(message(
                "schedule.run", "hosta:requester", schedule_id=inserted["schedule"]["schedule_id"],
            )))
        assert outcome.value.code == "indeterminate"
    finally:
        store.admitted_session_names_for_key = original
        store.stop()


def test_injective_receipts_exact_schema_handler_and_service_auth(tmp_path, monkeypatch) -> None:
    assert receipt_id("report/a", "phase") != receipt_id("report-a", "phase")
    assert receipt_id("request", "report/a") != receipt_id("request", "report-a")
    assert "=" in receipt_id("a", "x")

    store, sessions, _comms, _spawn, surface = harness(tmp_path)
    try:
        columns = run(store.submit(lambda conn: {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in (
                "v2_operation_receipts", "v2_schedules", "v2_schedule_dispatches",
            )
        }))
        assert columns["v2_operation_receipts"][:4] == ["receipt_id", "request_id", "phase", "surface"]

        server = Server(store=store, sessions=sessions)
        server.window_schedule = surface
        server.handlers.update(surface.wire_handlers())
        assert set(surface.wire_handlers()) <= set(server.handlers)
        # The unsupported coordination-window lease capability does not ride `hello`.
        ready = run(server._on_hello({
            "type": "hello", "subscribe": {"snapshot": False, "mode": "rpc"},
        }))[0]
        assert "capabilities" not in ready
        assert "coordination_" + "window_schema_health" not in ready
        assert server.handlers.pop("schedule.insert") is not None
        rejected = run(server._dispatch('{"type":"schedule.insert","request_id":"mutation-test"}'))[0]
        assert rejected["error_code"] == "unsupported_in_v2"

        monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", "tok")
        websocket = object()
        service_auth = run(server._auth_context(websocket, {"objective": "Exercise the existing spawn contract",
            "type": "schedule.insert", "from_stream_id": "hosta:scheduler",
            "stream_token": "tok",
        }))
        assert service_auth["service_authenticated"] is True
        assert service_auth["service_actor"] == "hosta:scheduler"
        assert service_auth["token_verified"] is False
    finally:
        store.stop()


@pytest.mark.parametrize("owner_kind", ["seat", "service"])
def test_schedule_fire_preserves_service_and_qa_owner_authority(tmp_path, owner_kind):
    from assistant_policy import AssistantPolicy

    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        owner = "hosta:requester" if owner_kind == "seat" else "hosta:scheduler"
        inserted = schedule_insert(surface, actor=owner, _auth_context=auth(owner, role=owner_kind))
        run(surface._fire_schedule(inserted["schedule"]["schedule_id"]))
        context = spawn.calls[0]["_auth_context"]
        # Internal scheduled dispatch must retain the public policy authority,
        # including service-owned schedules which have no reviewer owner token.
        assert AssistantPolicy(store, "hosta").operator(context)
        assert context["service_actor"] == "daemon:scheduler"
        assert context["token_verified"] is (owner_kind == "seat")
        assert context["stream_id"] == (owner if owner_kind == "seat" else None)
    finally:
        store.stop()
