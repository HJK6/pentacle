"""Unit tests for the ingest read/write path.

Covers the ingest pieces needed for a small, fast unit suite:
  * the store's `session_event_tail` append (insert + exactly-once dedup) and
    `fetch_session_event_tail` (newest-N, oldest-first, pre-existing rows);
  * the provider-jsonl normalizer's wire shapes;
  * the durable identity key's stability + dedup discrimination.
Each assertion covers a stable part of the public ingest contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import ingest as ingest_module
import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]

from claude_jsonl_norm import normalize_claude_jsonl_records  # noqa: E402
from ingest import Ingest, _StreamIngest, _identity_key, _jsonl_event_identity  # noqa: E402
from inventory import InventoryEmitter  # noqa: E402
from sessions import Sessions  # noqa: E402
from store import Store  # noqa: E402


def _open_store() -> Store:
    store = Store(":memory:")
    store.start()
    return store


async def _count_user_and_tell(store: Store, stream_id: str) -> int:
    host, separator, session_name = stream_id.partition(":")
    assert separator and host and session_name
    row = await store.fetch_session(host, session_name)
    assert row is not None
    lifecycle = str(row["created_at"])
    return sum(
        [
            await store.count_session_events(
                stream_id, kind=kind, session_created_at=lifecycle,
            )
            for kind in ("USER", "TELL")
        ]
    )


def test_append_inserts_then_dedups_by_identity() -> None:
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "s", visibility="visible")
            ev = {"stream_id": "h:s", "provider": "claude", "kind": "USER",
                  "text": "hi", "timestamp": "2026-08-05T00:00:00Z",
                  "raw": {"jsonl_record_uuid": "u1", "jsonl_event_index": 0}}
            ident = _identity_key(ev)
            first = await store.append_session_event("h:s", ev, identity=ident, limit=500)
            second = await store.append_session_event("h:s", ev, identity=ident, limit=500)
            # Returns the durable event_id (daemon_seq) on insert, None on replay.
            assert isinstance(first, int) and first > 0, "first append must insert (return its event_id)"
            assert second is None, "same record must not re-insert (exactly-once)"
            rows = await store.fetch_session_event_tail("h:s", limit=500)
            assert len(rows) == 1, rows
        finally:
            store.stop()

    asyncio.run(_go())


def test_fetch_returns_newest_n_oldest_first() -> None:
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "s", visibility="visible")
            for i in range(5):
                ev = {"stream_id": "h:s", "provider": "claude", "kind": "USER",
                      "text": f"m{i}", "timestamp": f"2026-08-05T00:00:0{i}Z",
                      "raw": {"jsonl_record_uuid": f"u{i}", "jsonl_event_index": 0}}
                await store.append_session_event("h:s", ev, identity=_identity_key(ev), limit=500)
            rows = await store.fetch_session_event_tail("h:s", limit=3)
            assert [r["text"] for r in rows] == ["m2", "m3", "m4"], rows  # newest 3, oldest-first
        finally:
            store.stop()

    asyncio.run(_go())


def test_fetch_hides_a_preexisting_row_without_the_current_lifecycle() -> None:
    """P0: blank-lifecycle history cannot render into an unrelated live stream."""
    async def _go() -> None:
        store = _open_store()
        try:
            # No open_session: the stream is not live, exactly like a closed
            # previous lifecycle session whose only trace is its durable tail.
            ev = {"stream_id": "h:gone", "provider": "claude", "kind": "ASSIST_TEXT",
                  "text": "old", "timestamp": "2026-01-01T00:00:00Z",
                  "raw": {"jsonl_record_uuid": "old1", "jsonl_event_index": 0}}
            assert await store.append_session_event("h:gone", ev, identity=_identity_key(ev), limit=500)
            rows = await store.fetch_session_event_tail("h:gone", limit=500)
            assert rows == []
        finally:
            store.stop()

    asyncio.run(_go())


def test_normalizer_maps_user_and_assistant_records() -> None:
    records = [
        {"type": "user", "uuid": "u1", "sessionId": "sid",
         "message": {"content": "hello there"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "sid",
         "message": {"content": [{"type": "text", "text": "hi back"}]}},
    ]
    events = normalize_claude_jsonl_records(records, host="h", session_name="s")
    kinds = [(e["kind"], e["text"]) for e in events]
    assert ("USER", "hello there") in kinds, kinds
    assert ("ASSIST_TEXT", "hi back") in kinds, kinds
    for e in events:  # wire shape: reader-facing keys are present
        for key in ("host", "provider", "stream_id", "kind", "text", "raw"):
            assert key in e, (key, e)


def test_synthetic_user_envelope_is_reclassified_system() -> None:
    records = [{"type": "user", "uuid": "u1", "sessionId": "sid",
                "message": {"content": "<system-reminder>noise</system-reminder>"}}]
    events = normalize_claude_jsonl_records(records, host="h", session_name="s")
    assert events and events[0]["kind"] == "SYSTEM", events


def test_identity_key_stable_and_discriminating() -> None:
    a = {"stream_id": "h:s", "provider": "claude", "kind": "USER", "text": "x",
         "raw": {"jsonl_record_uuid": "u1", "jsonl_event_index": 0}}
    b = {"stream_id": "h:s", "provider": "claude", "kind": "USER", "text": "x",
         "raw": {"jsonl_record_uuid": "u1", "jsonl_event_index": 1}}
    assert _identity_key(a) == _identity_key(dict(a))        # stable
    assert _identity_key(a) != _identity_key(b)              # index discriminates
    assert isinstance(_jsonl_event_identity(a), tuple)


# -- Codex rollout ingest ----------------------------------------------------
# These drive `_ingest_stream` directly with local synthetic records.

FIXTURES = SERVICE_DIR / "tests" / "fixtures"


class _StubSessions:
    """The two registry methods ingest actually calls."""

    @staticmethod
    def list_open() -> list[dict]:
        return []

    @staticmethod
    def split(stream_id: str) -> tuple[str, str]:
        host, name = stream_id.split(":", 1)
        return host, name

    @staticmethod
    def apply_durable(_stream_id: str, **_fields):
        return None

    @staticmethod
    def apply_genuine_activity_event(_stream_id: str, _event: dict) -> None:
        return None


def _codex_ingest(store: Store, broadcast=None) -> "Ingest":
    return Ingest(
        store, _StubSessions(), None, broadcast or (lambda _msg: asyncio.sleep(0)),
        local_host="h", recent_limit=500,
    )



async def _codex_row(store: Store, path: Path, stream_id: str = "h:v2-codex", *, pane_pid: str = "8123") -> dict:
    # Ingest receives hydrated durable inventory rows in production. Bind the
    # test's transcript there too, preserving the real generation/source fence.
    host, name = stream_id.split(":", 1)
    row = await store.update_session(host, name, provider="codex", jsonl_path=str(path))
    return {**row, "pane_pid": pane_pid}


def test_codex_stream_ingests_events_instead_of_returning_zero() -> None:
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            ingest = _codex_ingest(store)
            st = _StreamIngest()
            path = FIXTURES / "codex_rollout_first_turn.jsonl"
            appended = await ingest._ingest_stream(await _codex_row(store, path), st, 500)
            assert appended > 0, "a Codex stream must no longer ingest zero events"
            assert await _count_user_and_tell(store, "h:v2-codex") == 3
        finally:
            store.stop()

    asyncio.run(_go())


def test_ingest_advances_summary_only_from_persisted_turn_event() -> None:
    async def _go() -> tuple[float | None, str | None, list[dict]]:
        store = _open_store()
        broadcasts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            broadcasts.append(frame)

        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123", created_at="2024-12-31T00:00:00Z")
            sessions = Sessions(store, local_host="h")
            await sessions.refresh()
            ingest = Ingest(
                store,
                sessions,
                None,
                broadcast,
                local_host="h",
                recent_limit=500,
                inventory_emitter=InventoryEmitter(
                    sessions, broadcast, min_interval_s=0,
                ),
            )
            await ingest._ingest_stream(
                await _codex_row(store, FIXTURES / "codex_rollout_first_turn.jsonl"),
                _StreamIngest(),
                500,
            )
            row = sessions.get("h:v2-codex") or {}
            return (
                row.get("genuine_activity_at"),
                row.get("genuine_activity_generation"),
                broadcasts,
            )
        finally:
            store.stop()

    activity, generation, broadcasts = asyncio.run(_go())
    assert isinstance(activity, float)
    assert generation
    inventory = next(frame for frame in broadcasts if frame["type"] == "session.inventory")
    row = next(item for item in inventory["sessions"] if item["stream_id"] == "h:v2-codex")
    assert row["last_event_at"]


def test_codex_replay_from_start_appends_no_duplicates() -> None:
    """Exactly-once on Codex, the same floor the Claude path has: rediscovery
    replays from offset 0 and the durable identity must absorb it."""
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            ingest = _codex_ingest(store)
            path = FIXTURES / "codex_rollout_first_turn.jsonl"
            first = await ingest._ingest_stream(await _codex_row(store, path), _StreamIngest(), 500)
            replay = await ingest._ingest_stream(await _codex_row(store, path), _StreamIngest(), 500)
            assert first > 0 and replay == 0, (first, replay)
            assert await _count_user_and_tell(store, "h:v2-codex") == 3
        finally:
            store.stop()

    asyncio.run(_go())


def test_codex_foreign_transcript_is_rejected_not_silently_ingested() -> None:
    """The contamination guard reads `session_meta`; an alternate transcript
    format must not leave the identity check disarmed."""
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            ingest = _codex_ingest(store)
            st = _StreamIngest()
            st.session_id = "session-public-1"  # already bound
            foreign = FIXTURES / "codex_rollout_foreign_session.jsonl"
            row = await _codex_row(store, foreign)
            st.generation = row["session_generation"]
            appended = await ingest._ingest_stream(row, st, 500)
            assert appended == 0, "a foreign session's transcript must not be ingested"
            assert st.path == "", "the wrong binding must be dropped for re-discovery"
            assert await _count_user_and_tell(store, "h:v2-codex") == 0
        finally:
            store.stop()

    asyncio.run(_go())


def test_codex_first_bind_from_a_foreign_pane_is_rejected_before_broadcast() -> None:
    async def _go() -> None:
        store = _open_store()
        broadcasts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            broadcasts.append(frame)

        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            ingest = _codex_ingest(store, broadcast)
            foreign = FIXTURES / "codex_rollout_foreign_session.jsonl"
            appended = await ingest._ingest_stream(
                await _codex_row(store, foreign, pane_pid="7001"), _StreamIngest(), 500,
            )
            assert appended == 0
            assert broadcasts == []
            assert await store.fetch_session_event_tail("h:v2-codex", limit=500) == []
        finally:
            store.stop()

    asyncio.run(_go())


def test_codex_lifecycle_cas_loss_rejects_the_local_batch_without_broadcast() -> None:
    async def _go() -> None:
        store = _open_store()
        broadcasts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            broadcasts.append(frame)

        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")

            async def cas_loss(_entries, *, limit):
                return None

            store.append_session_events_lifecycle_cas = cas_loss  # type: ignore[method-assign]
            ingest = _codex_ingest(store, broadcast)
            appended = await ingest._ingest_stream(
                await _codex_row(store, FIXTURES / "codex_rollout_first_turn.jsonl"), _StreamIngest(), 500,
            )
            assert appended == 0
            assert broadcasts == []
            assert await store.fetch_session_event_tail("h:v2-codex", limit=500) == []
        finally:
            store.stop()

    asyncio.run(_go())


def test_codex_per_entry_drop_leaves_the_local_stream_unadvanced() -> None:
    """A local batch is one stream, so its per-entry drop is the whole verdict.

    The offset must stay put so the lines are re-read once a live pane rebinds
    the row, exactly as the pre-per-entry whole-batch rejection did.
    """
    async def _go() -> None:
        store = _open_store()
        broadcasts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            broadcasts.append(frame)

        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            lifecycle = await store.fetch_open_session_lifecycle("h:v2-codex", pane_pid="8123")
            assert lifecycle is not None
            original_append = store.append_session_events_lifecycle_cas

            async def close_after_admission(entries, *, limit):
                assert await store.mark_closed(
                    "h", "v2-codex", closed_at="2026-09-02T12:00:00Z",
                    pane_status="pane_dead", expected_generation=lifecycle["generation"],
                ) is not None
                return await original_append(entries, limit=limit)

            store.append_session_events_lifecycle_cas = close_after_admission  # type: ignore[method-assign]
            ingest = _codex_ingest(store, broadcast)
            state = _StreamIngest()
            appended = await ingest._ingest_stream(
                await _codex_row(store, FIXTURES / "codex_rollout_first_turn.jsonl"), state, 500,
            )
            assert appended == 0
            assert state.offset == 0
            assert broadcasts == []
            assert await store.fetch_session_event_tail("h:v2-codex", limit=500) == []
        finally:
            store.stop()

    asyncio.run(_go())


def test_codex_storage_failure_rejects_the_local_batch_without_broadcast() -> None:
    async def _go() -> None:
        store = _open_store()
        broadcasts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            broadcasts.append(frame)

        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")

            async def storage_failure(_entries, *, limit):
                raise OSError("forced lifecycle-CAS storage failure")

            store.append_session_events_lifecycle_cas = storage_failure  # type: ignore[method-assign]
            ingest = _codex_ingest(store, broadcast)
            appended = await ingest._ingest_stream(
                await _codex_row(store, FIXTURES / "codex_rollout_first_turn.jsonl"), _StreamIngest(), 500,
            )
            assert appended == 0
            assert broadcasts == []
            assert await store.fetch_session_event_tail("h:v2-codex", limit=500) == []
        finally:
            store.stop()

    asyncio.run(_go())


def test_unknown_provider_still_short_circuits() -> None:
    """Only providers with a normalizer written for them are parsed."""
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "v2-other", visibility="visible")
            ingest = _codex_ingest(store)
            row = {"stream_id": "h:v2-other", "provider": "gemini",
                   "jsonl_path": str(FIXTURES / "codex_rollout_first_turn.jsonl")}
            assert await ingest._ingest_stream(row, _StreamIngest(), 500) == 0
        finally:
            store.stop()

    asyncio.run(_go())


def test_larger_replacement_file_does_not_ingest_a_foreign_session(tmp_path: Path) -> None:
    """Replacing a bound transcript with a larger file must not bypass the
    identity guard when the read begins in the middle of that file."""
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            ingest = _codex_ingest(store)
            st = _StreamIngest()
            bound = tmp_path / "rollout.jsonl"
            bound.write_bytes((FIXTURES / "codex_rollout_first_turn.jsonl").read_bytes())

            first = await ingest._ingest_stream(await _codex_row(store, bound), st, 500)
            assert first > 0 and st.offset > 0

            # Replace in place with a different session's transcript, padded so
            # the new file is strictly larger than the consumed offset.
            foreign = (FIXTURES / "codex_rollout_foreign_session.jsonl").read_text()
            padding = "".join(
                '{"timestamp":"2026-08-08T19:22:0%d.000Z","type":"event_msg",'
                '"payload":{"type":"token_count"},"ordinal":%d}\n' % (i % 10, 100 + i)
                for i in range(200)
            )
            bound.unlink()
            bound.write_text(foreign + padding)
            assert bound.stat().st_size > st.offset, "the probe requires a LARGER replacement"

            appended = await ingest._ingest_stream(await _codex_row(store, bound), st, 500)
            assert appended == 0, "a replaced transcript must not be ingested blind"
            assert st.path == "", "the stale binding must be dropped"
            assert st.session_id, "the bound identity is KEPT so the re-bind must match it"

            rows = await store.fetch_session_event_tail("h:v2-codex", limit=500)
            texts = [r.get("text") or "" for r in rows]
            assert not any("another synthetic session" in t for t in texts), texts
        finally:
            store.stop()

    asyncio.run(_go())


def test_same_inode_in_place_replacement_is_rejected(tmp_path: Path) -> None:
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            ingest = _codex_ingest(store)
            st = _StreamIngest()
            bound = tmp_path / "rollout.jsonl"
            bound.write_bytes((FIXTURES / "codex_rollout_first_turn.jsonl").read_bytes())
            first = await ingest._ingest_stream(await _codex_row(store, bound), st, 500)
            assert first > 0 and st.offset > 0

            foreign = (FIXTURES / "codex_rollout_foreign_session.jsonl").read_text()
            bound.write_text(foreign + (" " * 2400))
            appended = await ingest._ingest_stream(await _codex_row(store, bound), st, 500)
            assert appended == 0
            assert st.path == ""
            assert await store.fetch_session_event_tail("h:v2-codex", limit=500)
        finally:
            store.stop()

    asyncio.run(_go())


def test_replacement_between_bind_and_read_is_rejected(tmp_path: Path, monkeypatch) -> None:
    async def _go() -> None:
        store = _open_store()
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            ingest = _codex_ingest(store)
            st = _StreamIngest()
            bound = tmp_path / "rollout.jsonl"
            bound.write_bytes((FIXTURES / "codex_rollout_first_turn.jsonl").read_bytes())
            first = await ingest._ingest_stream(await _codex_row(store, bound), st, 500)
            assert first > 0 and st.offset > 0

            foreign = (FIXTURES / "codex_rollout_foreign_session.jsonl").read_text()
            bound.write_bytes(bound.read_bytes() + b" \n")
            original_read = ingest_module._read_bound_span

            def replace_before_read(path, fd, start, end):
                bound.unlink()
                bound.write_text(foreign + (" " * 2400))
                return original_read(path, fd, start, end)

            monkeypatch.setattr(ingest_module, "_read_bound_span", replace_before_read)
            appended = await ingest._ingest_stream(await _codex_row(store, bound), st, 500)
            assert appended == 0
            assert st.path == ""
            assert await store.fetch_session_event_tail("h:v2-codex", limit=500)
        finally:
            store.stop()

    asyncio.run(_go())
