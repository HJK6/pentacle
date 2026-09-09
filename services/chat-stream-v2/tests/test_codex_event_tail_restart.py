"""Codex event tail re-attaches after a service restart (regression test).

A daemon restart rebuilds every `_StreamIngest` from scratch, so each open
Codex stream replays its transcript from offset 0 — the durable identity makes
that exactly-once. The bug: the local Codex ingest branch capped its per-pass
build on the number of entries BUILT (`len(entries) >= budget`), which counts
those durable replays, and then never advanced the offset when capped. So a
Codex seat whose replay span held more events than one pass's budget re-read the
same capped prefix forever and never reached its newer turns — `last_event_at`
froze at the restart while the pane kept working. The alternate provider branch
never stalled: it counts only genuine
inserts (`seq is not None`) against the budget, so durable replays are free.

This is the regression guard: with a history that exceeds one pass's budget, a
restart must still drain to the newest event. Discovery is out of scope here
(the row carries `jsonl_path`, the same convention the other codex ingest tests
use); the issue is the replay drain, not path binding.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ingest import Ingest, IngestConfig
from inventory import InventoryEmitter
from sessions import Sessions
from store import Store

SERVICE_DIR = Path(__file__).resolve().parents[1]
FIXTURES = SERVICE_DIR / "tests" / "fixtures"
#: The fixture's `session_meta` head fixes the bound session identity; appended
#: user turns share it so the whole file is one Codex session.
_SESSION_META = (FIXTURES / "codex_rollout_first_turn.jsonl").read_text().splitlines()[0]


def _user_turn(index: int) -> str:
    return json.dumps({
        "timestamp": f"2026-09-04T20:{index:02d}:00.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": f"msg_turn_{index}",
            "role": "user",
            "content": [{"type": "input_text", "text": f"operator turn {index}"}],
        },
        "ordinal": 100 + index,
    })


def _write_rollout(path: Path, turns: range) -> None:
    path.write_text(_SESSION_META + "\n" + "".join(_user_turn(i) + "\n" for i in turns))


def _ingest(store: Store, sessions: Sessions, broadcasts: list) -> Ingest:
    async def broadcast(frame: dict) -> None:
        broadcasts.append(frame)

    # A deliberately small per-pass budget so a modest history exceeds it, the
    # way a synthetic seat's accumulated transcript exceeds the configured budget.
    return Ingest(
        store, sessions, None, broadcast,
        local_host="h", recent_limit=500,
        config=IngestConfig(max_events_per_pass=3),
        inventory_emitter=InventoryEmitter(sessions, broadcast, min_interval_s=0),
    )


def test_codex_tail_resumes_after_restart_when_history_exceeds_budget(tmp_path: Path) -> None:
    async def _go() -> tuple[str | None, str | None, int]:
        store = Store(":memory:")
        store.start()
        try:
            roll = tmp_path / "rollout.jsonl"
            _write_rollout(roll, range(6))  # six historical turns > budget of 3
            await store.open_session(
                "h", "v2-codex", visibility="visible",
                provider="codex", pane_pid="8123", jsonl_path=str(roll),
                created_at="2026-09-04T19:00:00Z",
            )

            # -- first daemon lifetime: drain the history --
            sessions = Sessions(store, local_host="h")
            await sessions.refresh()
            ingest = _ingest(store, sessions, [])
            for _ in range(8):
                await ingest.run_pass()
            before = (sessions.get("h:v2-codex") or {}).get("last_event_at")

            # -- daemon RESTART: brand-new Sessions + Ingest, all offset state gone --
            sessions2 = Sessions(store, local_host="h")
            await sessions2.refresh()
            ingest2 = _ingest(store, sessions2, [])
            # new Codex turns land while/after the restart
            with roll.open("a") as fh:
                fh.write(_user_turn(6) + "\n")
                fh.write(_user_turn(7) + "\n")
            for _ in range(10):
                await ingest2.run_pass()

            row = await store.fetch_session("h", "v2-codex")
            ingested = await store.count_session_events(
                "h:v2-codex", kind="USER", session_created_at=str(row["created_at"]),
            )
            after = (sessions2.get("h:v2-codex") or {}).get("last_event_at")
            return before, after, ingested
        finally:
            store.stop()

    before, after, ingested = asyncio.run(_go())
    assert before, "sanity: history was mirrored before the restart"
    assert ingested == 8, f"every historical + new turn must be durable, got {ingested}/8"
    assert after == "2026-09-04T20:07:00.000Z", (
        f"after a restart last_event_at must advance to the newest Codex turn, got {after!r}"
    )
    assert after > before


def test_codex_pass_is_bounded_by_budget_and_does_not_starve_a_second_stream(tmp_path: Path) -> None:
    """The drain fix must not process a whole span in one pass (a burst that
    starves the other streams). It caps at `max_events_per_pass` entries and
    advances the offset over them, exactly like the Claude path — so one pass
    ingests at most the budget from a stream, later streams are deferred at most
    a pass (never permanently), and both eventually drain fully."""

    async def _go() -> dict[str, object]:
        store = Store(":memory:")
        store.start()
        try:
            roll_a = tmp_path / "a.jsonl"
            roll_b = tmp_path / "b.jsonl"
            _write_rollout(roll_a, range(5))  # 5 events, > budget of 3
            _write_rollout(roll_b, range(5, 7))  # 2 events
            # A opened first, so run_pass visits it first (order by created_at).
            await store.open_session("h", "v2-a", visibility="visible",
                                     provider="codex", pane_pid="8123", jsonl_path=str(roll_a))
            await store.open_session("h", "v2-b", visibility="visible",
                                     provider="codex", pane_pid="8124", jsonl_path=str(roll_b))
            sessions = Sessions(store, local_host="h")
            await sessions.refresh()
            ingest = _ingest(store, sessions, [])

            first = await ingest.run_pass()  # budget 3
            row_a = await store.fetch_session("h", "v2-a")
            row_b = await store.fetch_session("h", "v2-b")
            a_after_1 = await store.count_session_events(
                "h:v2-a", kind="USER", session_created_at=str(row_a["created_at"]))

            for _ in range(6):
                await ingest.run_pass()
            a_total = await store.count_session_events(
                "h:v2-a", kind="USER", session_created_at=str(row_a["created_at"]))
            b_total = await store.count_session_events(
                "h:v2-b", kind="USER", session_created_at=str(row_b["created_at"]))
            return {"first": first, "a_after_1": a_after_1, "a_total": a_total, "b_total": b_total}
        finally:
            store.stop()

    r = asyncio.run(_go())
    # One pass ingests at most the budget (3), not the whole 5-event span — no burst.
    assert r["first"] <= 3, f"a single pass must not exceed the budget, got {r['first']}"
    assert r["a_after_1"] == 3, f"stream A should ingest exactly the budget in pass 1, got {r['a_after_1']}"
    # Both streams drain fully across passes — the second is deferred, never starved.
    assert r["a_total"] == 5, r["a_total"]
    assert r["b_total"] == 2, r["b_total"]
