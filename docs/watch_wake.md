# Child watches and timed wakes

An authenticated caller can register a bounded wake or watch for its current session generation:

```sh
agent-orch wake --in 10m --note 'Check the fixture'
agent-orch wake list
agent-orch wake cancel <id>
agent-orch watch hosta:fixture-child --on end,blocker,idle,quiet=5 --repeat
agent-orch watch list
agent-orch watch cancel <id>
```

Wakes accept one positive duration or a future RFC3339 timestamp. Only an explicit urgent wake interrupts a busy turn; ordinary notices are queued. Watches target direct children and are scoped to the verified caller and generation.

| Trigger | Due condition | Re-arm |
|---|---|---|
| end | accepted completion, abort, or close | never in the same generation |
| blocker | accepted error report | after genuine activity or accepted progress |
| idle | working remains false past the configured threshold | after activity or a working transition |
| quiet | no genuine activity for the configured interval | after genuine activity |

Without repeat, a selected trigger consumes once. A single immutable outbox notice may satisfy subscriptions to the same fact. Closing a child cancels pending inactivity work; a final report may still reach the original parent generation. Reparenting retires the old nonterminal work and starts a fresh baseline.

Timers are evaluated after the ordinary reconciliation pass. A slow pass may delay enqueue; restart should enqueue an overdue active wake once for its live generation. Compare trigger, creation, and delivery timestamps to distinguish evaluation latency from delivery latency.

Tests should use synthetic ids, fixed clocks, and a disposable store. They should not depend on a private stream id, remote host, or deployment rollback command.
