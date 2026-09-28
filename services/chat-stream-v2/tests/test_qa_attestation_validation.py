"""Focused v2 QA-attestation control-plane coverage."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from websockets.sync.client import connect

from ledger import Ledger  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from store import Store  # noqa: E402
from tools.run_gate import _process_group_popen_kwargs, terminate_process_group  # noqa: E402


SPEC_ID = "spec_pentacle__daemon_v2_qa_attestation_validation_control_plane_2026_08"
PARENT = "alpha:parent"
READY = "alpha:ready"
QA = "alpha:qa"
SERVICE_DIR = Path(__file__).resolve().parents[1]
ITEM6_FOLDER_ALIAS = "spec_triforce-memory__process_doc_delta_batch_2026_08"
ITEM6_CANONICAL_ID = "spec_triforce_memory__process_doc_delta_batch_2026_08"
NON_ALIAS_HYPHEN = "spec_legal-alpha__identity"
NON_ALIAS_UNDERSCORE = "spec_legal_alpha__identity"


@pytest.fixture(autouse=True)
def _default_attestation_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENTACLE_QA_ATTESTATION_MODE", "warn")


async def _state():
    store = Store(":memory:")
    store.set_spec_identity_resolver(_test_canonical_identity)
    store.start()
    sessions = Sessions(store, local_host="alpha")
    await sessions.open("alpha", "parent", visibility="visible")
    for name, parent, spec_ids in (
        ("ready", PARENT, [SPEC_ID]),
        ("qa", None, [SPEC_ID, "spec_shared_secondary"]),
        ("other", None, [SPEC_ID]),
        ("mismatch", None, ["spec_unshared"]),
    ):
        await sessions.open(
            "alpha",
            name,
            visibility="hidden",
            parent_stream_id=parent,
            spec_id=spec_ids[0],
            spec_ids=spec_ids,
        )
    frames: list[dict] = []

    async def _broadcast(frame: dict) -> None:
        frames.append(frame)

    ledger = Ledger(store, sessions=sessions, broadcast=_broadcast)
    return store, sessions, Server(store=store, sessions=sessions), ledger, frames


def _report(
    report_id: str,
    stream_id: str,
    *,
    status: str = "done",
    qa_verdict: str | None = None,
    completion_kind: str | None = None,
    attestation: dict[str, str] | None = None,
) -> dict:
    message = {
        "report_id": report_id,
        "from_stream_id": stream_id,
        "msg_id": 7,
        "status": status,
        "summary": report_id,
    }
    if status == "done":
        message.update({"findings": [], "next_action": "complete"})
    if qa_verdict is not None:
        message["qa_verdict"] = qa_verdict
    if completion_kind is not None:
        message["completion_kind"] = completion_kind
    if attestation is not None:
        message["qa_attestation"] = attestation
    return message


def _canonical_item6_identity(spec_id: str | None) -> str | None:
    aliases = {
        ITEM6_FOLDER_ALIAS: ITEM6_CANONICAL_ID,
        ITEM6_CANONICAL_ID: ITEM6_CANONICAL_ID,
        NON_ALIAS_HYPHEN: NON_ALIAS_HYPHEN,
        NON_ALIAS_UNDERSCORE: NON_ALIAS_UNDERSCORE,
        "spec_pentacle__identity": "spec_pentacle__identity",
    }
    return aliases.get(str(spec_id or ""))


def _test_canonical_identity(spec_id: str | None) -> str | None:
    value = str(spec_id or "").strip().removeprefix("spec_")
    return f"spec_{value}" if value else None


def test_valid_attestation_is_durable_and_identical_on_every_v2_read_path() -> None:
    async def _go() -> None:
        store, _sessions, server, ledger, frames = await _state()
        try:
            await ledger.ingest(_report("qa-accept", QA, qa_verdict="accept"))
            reply = await ledger.report(_report(
                "ready-verified",
                READY,
                completion_kind="implementation_ready",
                attestation={"stream_id": QA, "report_id": "qa-accept"},
            ))
            expected = {
                "state": "verified",
                "reasons": [],
                "matched_spec_ids": [SPEC_ID],
                "qa_stream_id": QA,
                "qa_report_id": "qa-accept",
            }
            assert reply["qa_attestation_validation"] == expected
            notice = next(frame for frame in frames if frame["report_id"] == "ready-verified")
            assert notice["qa_attestation_validation"] == expected
            inspected = await server._on_inspect_stream({"stream_id": READY, "msg_id": 7, "event_tail": 0})
            assert inspected["existing_report"]["qa_attestation_validation"] == expected
            awaited = await ledger.await_report({"stream_id": READY, "msg_id": 7, "timeout": 0.01})
            assert awaited["report"]["qa_attestation_validation"] == expected
            assert awaited["qa_attestation_validation"] == expected
        finally:
            store.stop()

    asyncio.run(_go())


@pytest.mark.parametrize(
    ("reporter_spec", "qa_spec"),
    (
        ("spec_pentacle__identity", "pentacle__identity"),
        ("pentacle__identity", "spec_pentacle__identity"),
    ),
)
def test_canonical_and_bare_spec_ids_verify_for_independent_qa(
    reporter_spec: str, qa_spec: str,
) -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.set_spec_identity_resolver(_test_canonical_identity)
        store.start()
        sessions = Sessions(store, local_host="alpha")
        await sessions.open("alpha", "ready", visibility="hidden", spec_id=reporter_spec)
        await sessions.open("alpha", "qa", visibility="hidden", spec_id=qa_spec)
        ledger = Ledger(store, sessions=sessions)
        try:
            await ledger.ingest(_report("qa-identity", QA, qa_verdict="accept"))
            reply = await ledger.report(_report(
                "ready-identity",
                READY,
                completion_kind="implementation_ready",
                attestation={"stream_id": QA, "report_id": "qa-identity"},
            ))
            assert reply["qa_attestation_validation"] == {
                "state": "verified",
                "reasons": [],
                "matched_spec_ids": ["spec_pentacle__identity"],
                "qa_stream_id": QA,
                "qa_report_id": "qa-identity",
            }
        finally:
            store.stop()

    asyncio.run(_go())


def test_persisted_alias_bindings_verify_after_restart_without_historical_rewrite(tmp_path: Path) -> None:
    async def _go() -> None:
        database = tmp_path / "sessions.db"

        def binding(spec_id: str) -> dict:
            return {
                "spec_id": spec_id,
                "spec_ids": [spec_id],
                "qualified_spec_ids": [spec_id],
                "spec_binding_provenance": [{
                    "spec_id": spec_id,
                    "provenance": "spawn_explicit",
                    "granting_principal": "operator",
                    "granted_at": "2026-08-26T00:00:00Z",
                }],
            }

        store = Store(str(database))
        store.start()
        try:
            store.set_spec_identity_resolver(_canonical_item6_identity)
            sessions = Sessions(store, local_host="alpha")
            await sessions.open("alpha", "ready", visibility="hidden", **binding(ITEM6_FOLDER_ALIAS))
            await sessions.open("alpha", "qa", visibility="hidden", **binding(ITEM6_CANONICAL_ID))
            await sessions.open("alpha", "nonalias-ready", visibility="hidden", **binding(NON_ALIAS_HYPHEN))
            await sessions.open("alpha", "nonalias-qa", visibility="hidden", **binding(NON_ALIAS_UNDERSCORE))
            persisted = await store.fetch_session("alpha", "ready")
            assert persisted is not None
            assert persisted["qualified_spec_ids"] == [ITEM6_FOLDER_ALIAS]
        finally:
            store.stop()

        reopened = Store(str(database))
        reopened.start()
        try:
            reopened.set_spec_identity_resolver(_canonical_item6_identity)
            sessions = Sessions(reopened, local_host="alpha")
            ledger = Ledger(reopened, sessions=sessions)
            await ledger.ingest(_report("qa-item6", QA, qa_verdict="accept"))
            verified = await ledger.report(_report(
                "ready-item6",
                READY,
                completion_kind="implementation_ready",
                attestation={"stream_id": QA, "report_id": "qa-item6"},
            ))
            assert verified["qa_attestation_validation"] == {
                "state": "verified",
                "reasons": [],
                "matched_spec_ids": [ITEM6_CANONICAL_ID],
                "qa_stream_id": QA,
                "qa_report_id": "qa-item6",
            }

            await ledger.ingest(_report("qa-nonalias", "alpha:nonalias-qa", qa_verdict="accept"))
            distinct = await ledger.report(_report(
                "ready-nonalias",
                "alpha:nonalias-ready",
                completion_kind="implementation_ready",
                attestation={"stream_id": "alpha:nonalias-qa", "report_id": "qa-nonalias"},
            ))
            assert distinct["qa_attestation_validation"]["reasons"] == ["spec_binding_mismatch"]

            await sessions.open("alpha", "prefix-ready", visibility="hidden", **binding("pentacle__identity"))
            await sessions.open("alpha", "prefix-qa", visibility="hidden", **binding("spec_pentacle__identity"))
            await ledger.ingest(_report("qa-prefix", "alpha:prefix-qa", qa_verdict="accept"))
            prefix = await ledger.report(_report(
                "ready-prefix",
                "alpha:prefix-ready",
                completion_kind="implementation_ready",
                attestation={"stream_id": "alpha:prefix-qa", "report_id": "qa-prefix"},
            ))
            assert prefix["qa_attestation_validation"]["state"] == "verified"

            persisted = await reopened.fetch_session("alpha", "ready")
            assert persisted is not None
            assert persisted["qualified_spec_ids"] == [ITEM6_FOLDER_ALIAS]
        finally:
            reopened.stop()

    asyncio.run(_go())


@pytest.mark.parametrize("spec_id", ("spec_pentacle__identity", "pentacle__identity"))
def test_true_self_attestation_stays_unverified_for_every_spec_spelling(spec_id: str) -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.set_spec_identity_resolver(_test_canonical_identity)
        store.start()
        sessions = Sessions(store, local_host="alpha")
        await sessions.open("alpha", "ready", visibility="hidden", spec_id=spec_id)
        ledger = Ledger(store, sessions=sessions)
        try:
            await ledger.ingest(_report("qa-self-identity", READY, qa_verdict="accept"))
            reply = await ledger.report(_report(
                "ready-self-identity",
                READY,
                completion_kind="implementation_ready",
                attestation={"stream_id": READY, "report_id": "qa-self-identity"},
            ))
            assert reply["qa_attestation_validation"]["state"] == "unverified"
            assert reply["qa_attestation_validation"]["reasons"] == ["self_reference"]
        finally:
            store.stop()

    asyncio.run(_go())


@pytest.mark.parametrize(
    "failure_mode",
    ("missing_resolver", "zero_matches", "multiple_matches", "resolver_error"),
)
def test_attestation_never_matches_unresolved_or_ambiguous_bindings(
    failure_mode: str,
) -> None:
    async def _go() -> None:
        store = Store(":memory:")
        if failure_mode in {"zero_matches", "multiple_matches"}:
            store.set_spec_identity_resolver(lambda _spec_id: None)
        elif failure_mode == "resolver_error":
            def raise_resolution(_spec_id: str | None) -> str | None:
                raise RuntimeError("resolver unavailable")

            store.set_spec_identity_resolver(raise_resolution)
        store.start()
        try:
            sessions = Sessions(store, local_host="alpha")
            await sessions.open(
                "alpha", "ready", visibility="hidden", spec_id="spec_unresolved_identity",
            )
            await sessions.open(
                "alpha", "qa", visibility="hidden", spec_id="spec_unresolved_identity",
            )
            ledger = Ledger(store, sessions=sessions)
            await ledger.ingest(_report("qa-unresolved", QA, qa_verdict="accept"))
            reply = await ledger.report(_report(
                f"ready-unresolved-{failure_mode}",
                READY,
                completion_kind="implementation_ready",
                attestation={"stream_id": QA, "report_id": "qa-unresolved"},
            ))
            assert reply["qa_attestation_validation"] == {
                "state": "unverified",
                "reasons": ["spec_binding_mismatch"],
                "matched_spec_ids": [],
                "qa_stream_id": QA,
                "qa_report_id": "qa-unresolved",
            }
        finally:
            store.stop()

    asyncio.run(_go())


@pytest.mark.parametrize(
    ("label", "attestation", "qa_stream", "qa_status", "qa_verdict", "reasons"),
    (
        ("missing", None, None, None, None, ["missing_attestation"]),
        ("self", {"stream_id": READY, "report_id": "qa-self"}, READY, "done", "accept", ["self_reference"]),
        (
            "unknown-stream",
            {"stream_id": "alpha:missing", "report_id": "qa-missing"},
            None,
            None,
            None,
            ["unknown_qa_stream", "spec_binding_mismatch", "unknown_qa_report"],
        ),
        (
            "binding-mismatch",
            {"stream_id": "alpha:mismatch", "report_id": "qa-mismatch"},
            "alpha:mismatch",
            "done",
            "accept",
            ["spec_binding_mismatch"],
        ),
        ("unknown-report", {"stream_id": QA, "report_id": "qa-none"}, None, None, None, ["unknown_qa_report"]),
        (
            "wrong-owner",
            {"stream_id": QA, "report_id": "qa-other"},
            "alpha:other",
            "done",
            "accept",
            ["qa_report_stream_mismatch"],
        ),
        (
            "not-done",
            {"stream_id": QA, "report_id": "qa-progress"},
            QA,
            "progress",
            "accept",
            ["qa_report_not_done"],
        ),
        (
            "reject",
            {"stream_id": QA, "report_id": "qa-reject"},
            QA,
            "done",
            "reject",
            ["qa_verdict_not_accept"],
        ),
    ),
)
def test_warn_mode_persists_only_durable_unverified_reasons(
    label: str,
    attestation: dict[str, str] | None,
    qa_stream: str | None,
    qa_status: str | None,
    qa_verdict: str | None,
    reasons: list[str],
) -> None:
    async def _go() -> None:
        store, _sessions, _server, ledger, _frames = await _state()
        try:
            if qa_stream is not None:
                await ledger.ingest(_report(
                    attestation["report_id"], qa_stream, status=qa_status or "done", qa_verdict=qa_verdict,
                ))
            reply = await ledger.report(_report(
                f"ready-{label}", READY, completion_kind="implementation_ready", attestation=attestation,
            ))
            validation = reply["qa_attestation_validation"]
            assert validation["state"] == "unverified"
            assert validation["reasons"] == reasons
            assert reply["warnings"] == [{"code": "qa_attestation_unverified", "reasons": reasons}]
            assert "qa_report_lower_trust" not in validation["reasons"]
        finally:
            store.stop()

    asyncio.run(_go())


def test_enforce_rejects_before_insert_or_notification_and_replay_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _go() -> None:
        store, _sessions, _server, ledger, frames = await _state()
        try:
            with pytest.raises(VerbError) as raised:
                await ledger.report(_report("ready-enforced", READY, completion_kind="implementation_ready"))
            assert raised.value.code == "qa_attestation_unverified"
            assert await store.get_report("ready-enforced") is None
            assert frames == []

            await ledger.ingest(_report("qa-accept", QA, qa_verdict="accept"))
            first = await ledger.report(_report(
                "ready-replay", READY, completion_kind="implementation_ready",
                attestation={"stream_id": QA, "report_id": "qa-accept"},
            ))
            second = await ledger.report(_report(
                "ready-replay", READY, completion_kind="implementation_ready",
                attestation={"stream_id": QA, "report_id": "qa-accept"},
            ))
            assert second["qa_attestation_validation"] == first["qa_attestation_validation"]
            assert len([frame for frame in frames if frame["report_id"] == "ready-replay"]) == 1
        finally:
            store.stop()

    monkeypatch.setenv("PENTACLE_QA_ATTESTATION_MODE", "enforce")
    asyncio.run(_go())


def test_off_and_non_ready_leave_the_validation_field_null(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _go() -> None:
        store, _sessions, _server, ledger, _frames = await _state()
        try:
            off = await ledger.report(_report("ready-off", READY, completion_kind="implementation_ready"))
            progress = await ledger.report(_report("plain-progress", READY, status="progress"))
            assert off["qa_attestation_validation"] is None
            assert progress["qa_attestation_validation"] is None
        finally:
            store.stop()

    monkeypatch.setenv("PENTACLE_QA_ATTESTATION_MODE", "off")
    asyncio.run(_go())


def test_reporter_controlled_details_and_extras_never_supply_validation_facts() -> None:
    async def _go() -> None:
        store, _sessions, _server, ledger, _frames = await _state()
        try:
            qa = _report("qa-prose-only", QA)
            qa["details"] = {"qa_attestation_validation": {"state": "verified"}}
            await ledger.ingest(qa)
            ready = _report(
                "ready-prose-only",
                READY,
                completion_kind="implementation_ready",
                attestation={"stream_id": QA, "report_id": "qa-prose-only"},
            )
            ready["details"] = {"qa_attestation_validation": {"state": "verified"}}
            ready["extras"] = {"qa_attestation_validation": {"state": "verified"}}
            reply = await ledger.report(ready)
            assert reply["qa_attestation_validation"]["state"] == "unverified"
            assert reply["qa_attestation_validation"]["reasons"] == ["qa_verdict_not_accept"]
        finally:
            store.stop()

    asyncio.run(_go())


@pytest.mark.parametrize(
    ("provision_identity", "expected_state", "expected_reasons", "expected_matches"),
    (
        (True, "verified", [], [SPEC_ID]),
        (False, "unverified", ["spec_binding_mismatch"], []),
    ),
    ids=("temp-root-identity", "empty-temp-root-fails-closed"),
)
def test_isolated_daemon_bootstrap_attestation_is_resolver_hermetic(
    tmp_path: Path,
    provision_identity: bool,
    expected_state: str,
    expected_reasons: list[str],
    expected_matches: list[str],
) -> None:
    db = str(tmp_path / "bootstrap.db")
    # The subprocess must get its authority only from this fixture.  In
    # particular CI has no Triforce shared checkout, so provide the minimal
    # parser API and a temp work tree through its own paths.
    fixture_modules = tmp_path / "fixture-modules"
    fixture_modules.mkdir()
    (fixture_modules / "specs_parser.py").write_text(
        "DEFAULT_STATUSES = [{'name': 'in_progress', 'order': 1}]\n"
        "SYNTHETIC_SPEC_PROGRESS = {}\n"
        "def is_sync_conflict(_path):\n    return False\n"
        "def parse_statuses_json(_root):\n    return DEFAULT_STATUSES, {'in_progress': 1}\n"
        "def parse_work_folder(*_args):\n    raise AssertionError('not used by bootstrap identity fixture')\n",
        encoding="utf-8",
    )
    fixture_memory = tmp_path / "fixture-memory"
    fixture_work = fixture_memory / "work"
    fixture_work.mkdir(parents=True)
    (fixture_work / "statuses.json").write_text(
        json.dumps({"version": 2, "statuses": [{
            "name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False,
        }]}),
        encoding="utf-8",
    )
    if provision_identity:
        fixture_spec = fixture_work / "in_progress" / SPEC_ID
        fixture_spec.mkdir(parents=True)
        (fixture_spec / "spec.md").write_text(
            "---\n"
            f"id: {SPEC_ID}\n"
            "title: isolated bootstrap identity fixture\n"
            "---\n",
            encoding="utf-8",
        )

    async def _seed() -> None:
        store = Store(db)
        store.start()
        try:
            await store.open_session("alpha", "parent", visibility="visible")
            for name, parent, spec_ids in (
                ("ready", PARENT, [SPEC_ID]),
                ("qa", None, [SPEC_ID]),
            ):
                await store.open_session(
                    "alpha",
                    name,
                    visibility="hidden",
                    parent_stream_id=parent,
                    spec_id=spec_ids[0],
                    spec_ids=spec_ids,
                )
        finally:
            store.stop()

    asyncio.run(_seed())
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env["PENTACLE_MACHINES_JSON"] = '{"machines":[{"name":"alpha"}]}'
    env.pop("PENTACLE_MEMORY_ROOT", None)
    env["PENTACLE_MEMORY_ROOT"] = str(fixture_memory)
    # Do not inherit an ambient parser/check-out path: this is the permanent
    # CI-hermetic polarity. The empty temp root must fail closed.
    env["PYTHONPATH"] = str(fixture_modules)
    proc = subprocess.Popen(
        [
            sys.executable,
            "main.py",
            "--port",
            "0",
            "--db",
            db,
            "--local-host",
            "alpha",
            "--notifications-db",
            str(tmp_path / "notifications.db"),
            "--assets-db",
            str(tmp_path / "assets.db"),
            "--blob-root",
            str(tmp_path / "blobs"),
        ],
        cwd=str(SERVICE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **_process_group_popen_kwargs(),
    )
    try:
        assert proc.stdout is not None
        port = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "listening on" in line:
                port = int(line.rsplit(":", 1)[1].strip())
                break
        assert port is not None, f"bootstrap daemon did not bind (exit={proc.poll()})"

        def _request(ws, payload: dict, expected_type: str) -> dict:
            ws.send(json.dumps(payload))
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                reply = json.loads(ws.recv(timeout=5.0))
                if reply.get("type") == expected_type:
                    return reply
            raise AssertionError(f"missing {expected_type} reply")

        expected = {
            "state": expected_state,
            "reasons": expected_reasons,
            "matched_spec_ids": expected_matches,
            "qa_stream_id": QA,
            "qa_report_id": "bootstrap-qa-accept",
        }
        with connect(f"ws://127.0.0.1:{port}", open_timeout=5.0) as observer, \
                connect(f"ws://127.0.0.1:{port}", open_timeout=5.0) as reporter:
            _request(observer, {"type": "hello", "request_id": "bootstrap-observer"}, "hello")
            _request(reporter, {
                "type": "hello",
                "request_id": "bootstrap-reporter",
                "subscribe": {"include_subagents": True},
            }, "hello")
            qa = _request(
                reporter,
                {"type": "report", "request_id": "bootstrap-qa", **_report("bootstrap-qa-accept", QA, qa_verdict="accept")},
                "report.ok",
            )
            assert qa["report_id"] == "bootstrap-qa-accept"
            ack = _request(
                reporter,
                {
                    "type": "report",
                    "request_id": "bootstrap-ready",
                    **_report(
                        "bootstrap-ready",
                        READY,
                        completion_kind="implementation_ready",
                        attestation={"stream_id": QA, "report_id": "bootstrap-qa-accept"},
                    ),
                },
                "report.ok",
            )
            assert ack["qa_attestation_validation"] == expected
            notice = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                frame = json.loads(observer.recv(timeout=5.0))
                if frame.get("type") == "child_report_ready" and frame.get("report_id") == "bootstrap-ready":
                    notice = frame
                    break
            assert notice is not None
            assert notice["qa_attestation_validation"] == expected
            inspect = _request(
                reporter,
                {"type": "inspect_stream", "request_id": "bootstrap-inspect", "stream_id": READY, "msg_id": 7, "event_tail": 0},
                "inspect_stream.ok",
            )
            assert inspect["existing_report"]["qa_attestation_validation"] == expected
            awaited = _request(
                reporter,
                {"type": "await_report", "request_id": "bootstrap-await", "stream_id": READY, "msg_id": 7, "timeout": 1.0},
                "await_report.ok",
            )
            assert awaited["report"]["qa_attestation_validation"] == expected
            assert awaited["qa_attestation_validation"] == expected
    finally:
        terminate_process_group(proc)
