"""QA lineage: a verified host-transfer successor of an authorizing ancestor.

Mirrors the real topology: two closed coordinator leads under a closed FD,
three counted rejects, and the FD's successors linked by completed transfers.
"""
import asyncio
import hashlib
import json

import pytest

import store_qa
from test_qa_dispatch_counter import SPEC, LEAD, TOKEN, state, send_msg, report_msg, issue

OLD_FD = "localhost:fdold"
LEAD2 = "localhost:lead2"
TOKEN2 = "qa-lineage-lead2-token"
MID = "localhost:fdmid"
FD = "localhost:fd"
FD_TOKEN = "qa-lineage-fd-token"


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def _generation(store, stream):
    host, name = stream.split(":", 1)
    return (await store.fetch_session(host, name))["session_generation"]


async def _transfer(store, command, edges, state_="complete", ready=1):
    rows = []
    for source, target in edges:
        rows.append((command, source, await _generation(store, source),
                     target, await _generation(store, target), ready))

    def write(conn):
        conn.execute(
            "INSERT INTO v2_host_transfers(command_id,composite,input_identity,dispatch_id,"
            "digest,state,expires_at,data) VALUES(?,?,?,?,?,?,?,?)",
            (command, "localhost:assistant", command, command, "d" * 64, state_, 0.0, "{}"))
        conn.executemany(
            "INSERT INTO v2_host_transfer_seats(command_id,source,source_generation,"
            "target,target_generation,ready) VALUES(?,?,?,?,?,?)", rows)
        conn.commit()
    await store.submit(write)


async def topology(**variant):
    """Build the counted rejects and the FD chain; `variant` breaks one link."""
    store, sessions, _, comms, ledger, server = await state()
    await sessions.open("localhost", "fdold", role="lead", spec_id=SPEC, spec_ids=[SPEC])
    await store.update_session("localhost", "lead", parent_stream_id=OLD_FD)
    await sessions.open("localhost", "lead2", role="lead", spec_id=SPEC, spec_ids=[SPEC],
                        parent_stream_id=OLD_FD, token_hash=_hash(TOKEN2),
                        token_hash_version="sha256:v1")
    await sessions.open("localhost", "qa4", role="qa", provider="claude", parent_stream_id=LEAD2,
                        spec_id=SPEC, spec_ids=[SPEC], bootstrap_state="ready")
    # Three commissions are admitted before any report lands, so all three count.
    commissions = (("qa1", LEAD, TOKEN), ("qa2", LEAD, TOKEN), ("qa4", LEAD2, TOKEN2))
    for name, sender, token in commissions:
        sent = await comms.send(send_msg(name, from_stream_id=sender, stream_token=token))
        assert sent["delivery"] == "landed", sent
    for name, _, _ in commissions:
        await ledger.ingest(report_msg(name))
    for name, sender, token in commissions:
        result = await issue(server, "adjudicate", report_id=f"reject-{name}", adjudicated_valid=True,
                             reason="valid", from_stream_id=sender, stream_token=token)
        assert result["type"].endswith(".ok"), result
    for name in ("lead", "lead2"):
        await store.update_session("localhost", name, status="closed")

    first = variant.get("mid_handoff_from", OLD_FD)
    await sessions.open("localhost", "fdmid", role="lead", spec_id=SPEC, spec_ids=[SPEC],
                        handoff_from_stream_id=first)
    await sessions.open("localhost", "fd", role="lead", spec_id=SPEC, spec_ids=[SPEC],
                        handoff_from_stream_id=variant.get("fd_handoff_from", MID),
                        token_hash=_hash(FD_TOKEN), token_hash_version="sha256:v1")
    await store.update_session("localhost", "fdold", status="closed")
    await store.update_session("localhost", "fdmid", status="closed")
    # The real chain also carries a failed attempt from the old FD.
    await sessions.open("localhost", "fdfailed", role="lead", spec_id=SPEC)
    await _transfer(store, "move-failed", [(OLD_FD, "localhost:fdfailed")], state_="failed")
    if not variant.get("no_first_transfer"):
        await _transfer(store, "move", [(OLD_FD, MID)], state_=variant.get("first_state", "complete"),
                        ready=variant.get("first_ready", 1))
    await _transfer(store, "return", [(MID, FD)])
    if variant.get("stale_source"):
        await store.submit(lambda c: (c.execute(
            "UPDATE v2_host_transfer_seats SET source_generation='stale' WHERE command_id='move'"), c.commit()))
    if variant.get("stale_target"):
        await store.submit(lambda c: (c.execute(
            "UPDATE v2_host_transfer_seats SET target_generation='stale' WHERE command_id='return'"), c.commit()))
    return store, sessions, server


async def _lineage(store, actor_generation=None, actor_stream=FD):
    def check(conn):
        actor = store_qa._session(conn, actor_stream)
        if actor_generation is not None:
            actor = dict(actor, session_generation=actor_generation)
        rejects = store_qa._rejects(conn, SPEC, "admission", 1)
        return [store_qa._authorized_lineage(conn, actor, r) for r in rejects]
    return await store.submit(check)


async def _diagnose(server, **fields):
    return await issue(server, "diagnose", diagnosis_id="lineage-pivot", diagnosis="wrong abstraction",
                       pivot="different acceptance scope", from_stream_id=FD, stream_token=FD_TOKEN, **fields)


def test_real_topology_fd_successor_diagnoses_all_three(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, server = await topology()
        try:
            assert await _lineage(store) == [True, True, True]
            result = await _diagnose(server)
            assert result["type"] == "coordination.spec_issue.diagnose.ok", result
            assert sorted(json.loads(await store.submit(lambda c: c.execute(
                "SELECT report_ids FROM v2_qa_diagnoses WHERE diagnosis_id='lineage-pivot'"
            ).fetchone()[0]))) == ["reject-qa1", "reject-qa2", "reject-qa4"]
        finally:
            store.stop()

    asyncio.run(run())


def test_one_edge_verified_successor_of_ancestor_diagnoses(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, server = await topology()
        try:
            await sessions.open("localhost", "fdone", role="lead", spec_id=SPEC, spec_ids=[SPEC],
                                handoff_from_stream_id=OLD_FD, token_hash=_hash("fdone-token"),
                                token_hash_version="sha256:v1")
            await _transfer(store, "move-one", [(OLD_FD, "localhost:fdone")])
            assert await _lineage(store, actor_stream="localhost:fdone") == [True, True, True]
            result = await issue(server, "diagnose", diagnosis_id="one-edge-pivot", diagnosis="wrong abstraction",
                                 pivot="different acceptance scope", from_stream_id="localhost:fdone",
                                 stream_token="fdone-token")
            assert result["type"] == "coordination.spec_issue.diagnose.ok", result
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("variant", [
    {"no_first_transfer": True},           # missing transfer evidence
    {"first_state": "failed"},             # only a failed transfer
    {"first_ready": 0},                    # complete transfer, seat not ready
    {"stale_source": True},                # recycled/stale source generation
    {"stale_target": True},                # stale target generation
    {"fd_handoff_from": "localhost:fdfailed"},  # pointer names a different predecessor
    {"mid_handoff_from": "localhost:fdfailed"},  # intermediate pointer mismatch
])
def test_unproven_custody_is_refused(monkeypatch, variant):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, server = await topology(**variant)
        try:
            assert await _lineage(store) == [False, False, False]
            result = await _diagnose(server)
            assert result.get("error_code") == "qa_unauthorized", result
        finally:
            store.stop()

    asyncio.run(run())


def test_stale_live_actor_generation_is_refused(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _ = await topology()
        try:
            assert await _lineage(store, actor_generation="previous-generation") == [False, False, False]
        finally:
            store.stop()

    asyncio.run(run())


def test_recycled_coordinator_generation_is_refused(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _ = await topology()
        try:
            await store.submit(lambda c: (c.execute(
                "UPDATE v2_session_generations SET generation='recycled' WHERE host='localhost' AND session_name='lead'"),
                c.commit()))
            # lead's two commissions lose their anchor; lead2's commission still holds.
            assert await _lineage(store) == [False, False, True]
        finally:
            store.stop()

    asyncio.run(run())


def test_partial_ownership_refuses_diagnosis(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, server = await topology()
        try:
            # lead2 is re-parented away from the old FD: the FD owns only two of three.
            await store.update_session("localhost", "lead2", parent_stream_id="localhost:elsewhere")
            assert await _lineage(store) == [True, True, False]
            assert (await _diagnose(server)).get("error_code") == "qa_unauthorized"
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("relation", ["sibling", "spec_only"])
def test_unrelated_actor_is_refused(monkeypatch, relation):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, server = await topology()
        try:
            fields = {"parent_stream_id": OLD_FD} if relation == "sibling" else {}
            await sessions.open("localhost", "other", role="lead", spec_id=SPEC, spec_ids=[SPEC],
                                token_hash=_hash("other-token"), token_hash_version="sha256:v1", **fields)
            result = await issue(server, "diagnose", diagnosis_id="other-pivot", diagnosis="wrong abstraction",
                                 pivot="different acceptance scope", from_stream_id="localhost:other",
                                 stream_token="other-token")
            assert result.get("error_code") == "qa_unauthorized", result
        finally:
            store.stop()

    asyncio.run(run())


def test_cyclic_chain_with_proven_edges_is_refused(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _ = await topology()
        try:
            # Both edges of fd <-> fdmid carry full custody, so only the cycle guard refuses.
            fd_generation = await _generation(store, FD)
            await store.update_session("localhost", "fdmid", handoff_from_stream_id=FD)
            await store.submit(lambda c: (c.execute(
                "UPDATE v2_host_transfer_seats SET source=?, source_generation=? WHERE command_id='move'",
                (FD, fd_generation)), c.commit()))
            assert await asyncio.wait_for(_lineage(store), 10) == [False, False, False]
        finally:
            store.stop()

    asyncio.run(run())


def test_broken_chain_is_refused(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _ = await topology()
        try:
            await store.update_session("localhost", "fdmid", handoff_from_stream_id="localhost:gone")
            assert await _lineage(store) == [False, False, False]
        finally:
            store.stop()

    asyncio.run(run())


def test_closed_actor_is_refused_by_existing_gate(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, server = await topology()
        try:
            await store.update_session("localhost", "fd", status="closed")
            result = await _diagnose(server)
            assert result["type"].endswith(".error"), result
        finally:
            store.stop()

    asyncio.run(run())


def test_duplicate_target_seat_is_schema_refused(monkeypatch):
    """One seat row per target, so an exact P->S edge cannot be ambiguous."""
    import sqlite3
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _ = await topology()
        try:
            def duplicate(conn):
                conn.execute(
                    "INSERT INTO v2_host_transfer_seats(command_id,source,source_generation,target,"
                    "target_generation,ready) VALUES('other','localhost:lead2','g',?,'g',1)", (FD,))
            with pytest.raises(sqlite3.IntegrityError):
                await store.submit(duplicate)
        finally:
            store.stop()

    asyncio.run(run())
