# Front-desk turn-budget observation

The daemon observes only the currently bound direct-primary assistant. Defaults
are five own tool calls, 60 seconds of own work, and **notice delivery off**.
This is advisory: it does not interrupt, stop, pause, kill, delegate, change a
status card, or enforce the existing delegation rule.

## Configuration

Configure the existing direct-primary assistant normally, then use:

- `PENTACLE_FD_TURN_BUDGET_CALLS=5`
- `PENTACLE_FD_TURN_BUDGET_SECONDS=60`
- `PENTACLE_FD_TURN_BUDGET_ENABLED=false`

These are daemon startup settings. Invalid values fail startup. Enabling is a
separate operator/integration decision. Disabling stops delivery, not observer
cost or durable observation. Roll back faulty source to remove observer cost;
retain the database and archive so crossing identities remain durable.

## What counts

A proved distinct inbound USER or inbound TELL begins a turn. The immutable key
is `[stream_id, actual_session_generation, ordinal]`. Same-generation restart
and replay preserve the existing cursor. An actual new authoritative generation
drops unfinished work. A proved new origin starts at ordinal one; late binding
with earlier unobserved events reports a history gap. Different streams never share
a key even when their generation strings match.

Count distinct normalized `raw.tool_use_id` values, not shell subcommands,
results, or thinking. Own time runs from the first ASSIST_TEXT or TOOL_USE
timestamp to the latest ASSIST_TEXT, TOOL_USE, or TOOL_RESULT timestamp. Equal
timestamps are stable. Strictly more than the configured budget crosses it;
exactly five calls or exactly 60 seconds does not. Calls is the dimension when
both thresholds first cross on the same event.

The accepted-event transaction commits the small existing `kv` cursor, one
immutable `fd_turn_budget` event in `session_event_tail`, and, when enabled, one
existing outbound notice. Rollback removes all three effects. Disabled crossings
remain record-only forever; enabling later does not notify them. The next turn
can independently cross and notify.

The notice text is:

> Turn budget crossed (N calls / S s). Delegate the remainder and end the turn.

Existing marker, tell ID, receipt, retry and generation-fenced input handling are
reused. The exact internal `fd_turn_budget` kind is eligible for ordinary
nonurgent delivery immediately on crossing, even with the default front-desk
digest enabled. It does not wait for the periodic digest; existing scheduling,
authentication, retained-tell conflicts and generation checks still apply.
Unrelated ordinary notices retain their existing digest policy. Delivery repeats the exact direct-binding generation/revision check
under the existing binding and lifecycle locks. A stale or disabled queued
notice is terminally suppressed, never redirected. A delivered notice itself
opens one zero-work turn, clearing the preceding count/span; it cannot trigger
itself.

## Evidence gaps and recovery

A TELL label or sender-looking text is not proof. Provider-native directed
Codex agent messages retain their existing direction interpretation. Peer and
notice envelopes require matching existing durable provenance, target and body.
Historical Claude queued peers remain USER; the normalizers are unchanged.
SYSTEM/developer setup, sidechains, outbound agent messages and assistant
quotations never become inbound boundaries.

A legacy generation with no trusted cursor cannot safely infer ordinal one from
the oldest surviving USER. It reports `ordinal_history_unproven`. A corrupt
cursor, missing previously observed/configured archive, unreadable history,
unknown compaction/transcript identity or unobserved history continuity likewise
remains an explicit gap. The observer does not claim protection for that span.
A genuine new generation observed before its events provides a new origin;
existing proved current-turn state survives same-generation restart/rebind.

Ordinary tell provenance can arrive after its transcript event. The observer
retains its last proved small checkpoint and marks `peer_provenance_pending`.
Receipt completion or subsequent accepted source work can reconcile at most
128 retained rows against the now-durable receipt. The bounded checkpoint
retains source identity/content proofs and each event's enabled state; missing
prefixes/interior rows, changed content and independent archive gaps cannot be
cleared by recovery. It never guesses through unresolved proof.
Recovered earlier crossings do not send late notices for completed/preactivation
turns. A still-current turn that crossed while enabled can emit its one notice
when proof arrives. Missing or oversized recovery/prior-dispatch history is an
explicit gap; no full-history reconstruction runs on the Store thread.

Archive identity/provenance probes run off the main and Store threads, are
read-only, and have bounded work/time. They cannot create a missing archive.
Only current-turn tool IDs, at most the call budget plus one, are retained;
after crossing further calls cannot generate another trigger.

## Retro and validation

Run the existing collector on demand in the same session:

```
python services/chat-stream-v2/tools/daily_retro.py collect \
  --config /absolute/path/to/retro.json --now 2026-10-10T05:00:00-05:00
```

The collector's primary observations combine readable hot/archive budget events
by immutable identity before time filtering and limits. `primary.turns_over_budget`
contains count, bounded turn IDs, Chicago-local day counts and completeness.
Unavailable hot storage does not hide readable archive evidence. The canonical
aware timestamp parser supplies exact window bounds. Overflow, conflicting/
malformed rows and unavailable stores remain explicit;
partial counts are observed lower bounds, not complete totals. No transcript or
notice body is copied into budget observations. Existing row/byte budgets apply.
The collector caches each local day; a controlled synthetic acceptance journey
must use a fresh isolated state root to avoid reading an earlier same-day packet.

Focused tests are in `tests/test_fd_turn_budget.py` and `tests/test_daily_retro.py`.
`tests/smoke/test_fd_turn_budget_socket.py` drives an authenticated private
`event.push` socket, synthetic provider input through real Comms/proof, replay,
and the immediate collector CLI. It checks default-digest-on delivery, disabled
record-only mode, stale revision/generation suppression, the unchanged ordinary
notice policy, and a replay-safe zero-work next turn. The existing merge gate selects these tests.

Source/harness proof is not deployed acceptance. Installation, activation and a
controlled six-command journey on the intended daemon remain separately gated.
Read back the event, one notice and its tell/turn IDs, then the on-demand primary
retro packet. Preserve the configuration/release preimage and rollback pointer.
