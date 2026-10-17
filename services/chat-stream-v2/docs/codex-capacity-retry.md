# Codex capacity continuation

A source-confirmed Codex terminal `server_overloaded` error schedules a
nonurgent `continue` no earlier than 60 seconds after admission. Delivery runs
on the next existing reconciler pass; its cadence and reconciliation duration
can add latency. No second poller runs.

One unresolved episode permits at most three possible submissions. Each later
attempt needs a distinct new capacity error following native USER proof of the
preceding attempt. The automatic echo does not reset the budget. Genuine
provider work clears the episode; ordinary incoming input cancels a pending
episode. Other provider errors never trigger this behavior.

Before input, the original seat generation, process, native session and exact
final terminal record must still match. Unknown/offline/busy observations,
nonempty drafts, queued input, reset dialogs, changed panes/files and ambiguous
open rollouts defer without input. The bounded native probe returns metadata
only. It uses the same process/start and transcript identity conventions as
existing ingest and requires `ps`, `lsof` and Python on the target host.

Deadlines, reservations, consumed error identities and holds persist in the
existing database. A possible submission is charged before input. Restart or
late/missing proof never authorizes another paste; uncertainty is shown for
manual action. Desktop/web session status displays waiting/deadline, attempts
used out of three, submission state and exhaustion, including sessions without
an authored status card. Exhaustion reuses the existing parent notice outbox.

## Explicit controls

Authenticated operators and the current lifecycle authority manager can use:

```sh
agent-orch capacity-retry hold --all
agent-orch capacity-retry status --all
agent-orch capacity-retry resume --all
agent-orch capacity-retry hold --target HOST:SEAT --generation GENERATION
agent-orch capacity-retry resume --target HOST:SEAT --generation GENERATION
```

The wire equivalent is `capacity_retry.control`, with `action` of `hold`,
`resume` or `status`, a `request_id`, and exactly one of `all: true` or
`target_stream_id` plus `expected_session_generation`. Caller authority comes
from the authenticated connection. Ordinary seat tokens and caller-supplied
operator flags cannot control it. Mutation request IDs are durable and cannot
be reused for a different command.

Holds inhibit before acknowledgement, then drain any input already in flight.
No automatic paste starts after the hold acknowledgement. Supported UI
interrupts latch a generation hold before Escape, even if Escape delivery is
uncertain. Close invalidates the generation. New input does not lift a hold;
only explicit resume does. Resume consumes the existing error frontier and
cannot revive an old error. Explicit resume allows a distinct new capacity
stop while retaining the unresolved episode attempt count. Other cancelled episodes remain cancelled
until verified provider progress. Unknown composer/footer layouts defer input.

These controls do not parse natural-language STOP instructions and do not
implement the legacy generic park/hold verbs. A direct physical keyboard is not
serialized by daemon locks; native input/draft checks are the available proof.

Before rollback, hold all retries and inspect the acknowledged hold, then
restore the prior source/runtime artifact. Preserve the database's receipts and
consumed frontiers across disable/re-enable. Runtime activation remains a
separate release-owner operation.

Focused validation is in `test_codex_capacity_retry.py`,
`test_codex_capacity_probe.py` and `test_codex_capacity_native_journey.py`.
The last test owns an isolated tmux socket and inert transcript writer and
checks three physical submissions, durable receipts and rendered status. It
never invokes a provider binary or account.
