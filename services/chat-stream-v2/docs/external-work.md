# External-work check

Some work runs outside the fleet, where the daemon cannot see it. The front
desk owns that queue. This check keeps two deadlines for it and reminds the
currently bound front desk when one passes: verify the work at least every two
hours, and act within one hour when it is idle or waiting on the fleet.

The daemon stores what the front desk attests and when. It does not open the
queue, read mail, choose a task, send a packet or call a model. A reminder
records nothing; only a front-desk record changes the state.

Code: `external_work.py` (validation, deadlines, wire verbs, delivery guard),
`store_external_work.py` (transactions), `agent_orch/external_work_cli.py`.

## Configuration

`PENTACLE_EXTERNAL_WORK_CONFIG` names a local JSON file, read once at daemon
start. Unset disables the check. One watch is supported.

```json
{"v": 1, "watch_id": "example-watch", "label": "Example worker",
 "queue_ref": "path/or/url/of/the/queue", "assistant_name": "<primary assistant>"}
```

| Field | Rule |
| --- | --- |
| `v` | `1` |
| `watch_id` | `[a-z0-9][a-z0-9_-]{0,63}` |
| `label` | nonblank, at most 80 characters |
| `queue_ref` | nonblank opaque reference, at most 512 characters |
| `assistant_name` | the primary assistant's name (the unprefixed `PENTACLE_ASSISTANT_*` composite) |

An unreadable file, unknown key, wrong type or wrong version logs
`subsystem=external_work error=config_invalid action=disabled` and leaves the
check off. The daemon still starts. Neither the path nor the content is logged.

Disabling keeps the stored state, and a queued reminder is refused while the
check is off. Enabling the same `watch_id` again resumes with the same ages. A
different `watch_id` starts a new check that is due at once.

## Verbs

```
agent-orch external-work show
agent-orch external-work record --file PATH
```

Both print the daemon's JSON reply and exit nonzero on a refusal. They use the
calling seat's existing stream token. The daemon admits only the configured
assistant's current front desk at its current open generation, and rechecks
that binding inside the write transaction. A successor front desk uses its own
token.

The record file holds exactly these fields, at most 16 KiB in total:

| Field | Rule |
| --- | --- |
| `watch_id` | the configured watch |
| `request_id` | nonblank, at most 128 characters; the idempotency key |
| `expected_version` | the `version` from the last `show` or `record` |
| `observed_at` | epoch seconds; not in the future, at most 900 s old, not before the previous observation |
| `state` | `working`, `idle`, `waiting_on_fleet` or `unknown` |
| `current_packet_ref` | `null` or a reference; required for `working` |
| `evidence_refs` | up to 10 distinct references; at least one unless `unknown` |
| `queue_sha256` | SHA-256 of the queue snapshot the front desk read |
| `next_packet_ref` | `null` or the ready next packet |
| `supply_gap` | `null` or why no packet is ready; exactly one of this and `next_packet_ref` is set |
| `blocker` | `null` or `{"owner", "reason"}`; required for `waiting_on_fleet` |

References are at most 512 characters. They are front-desk attestations: the
daemon checks their shape, not whether they are true.

A successful reply is `{type, ok, watch_id, version, state, health}`; `record`
adds `request_id` and `duplicate`. `state` is the whole durable state.
`health` holds `enabled`, `due_at`, `reasons` and `last_notice` (the outbox
status of the current reminder, or `null`).

Repeating a `request_id` with the same payload returns the stored reply with
`duplicate: true` and changes nothing. A different payload under the same
`request_id` is `idempotency_conflict`.

| `error_code` | Meaning |
| --- | --- |
| `not_authenticated` | no verified token for an open seat |
| `fd_not_current` | the caller is not the current front desk |
| `disabled` / `config_invalid` | the check is off |
| `invalid_request` | malformed record, or `observed_at` in the future |
| `observation_stale` | `observed_at` is over 900 s old or earlier than the last one |
| `idempotency_conflict` | `request_id` reused with another payload |
| `version_conflict` | `expected_version` is not the current version |
| `unavailable` | the store could not serve the request |

## Deadlines

Reasons are derived on every tick, `show` and `record`:

| Reason | Active when |
| --- | --- |
| `check_due` | never verified, or two hours since the last verified observation |
| `action_due` | one hour since the work became idle or waiting |
| `state_unknown` | no observation yet, or the latest is `unknown` |
| `supply_gap` | no observation yet, or the latest names no next packet |

A known state sets the verified time to `observed_at`. `unknown` never does.
Entering `idle` or `waiting_on_fleet` starts the one-hour clock; repeating
either, switching between them or reporting `unknown` keeps it. Only a
`working` observation clears it.

The daemon compares against the highest clock it has seen, so a backward
clock jump cannot hide work that is already due. A forward jump can raise a
reminder early; it never creates a verification.

## Reminders

The check runs on the existing five-second outbox pass, in one store
transaction, before notices are claimed. A failure is logged and does not stop
other notices.

Reasons going from none to some open an episode. One reminder
(`external_work_due`) is queued for the current front desk at once, and the
next no sooner than two hours after the last. Reasons changing inside an
episode add no reminder. When every reason clears, the episode closes and any
undelivered reminder is retired.

A reminder passes the front-desk digest without its hold and does not
interrupt a running turn. A wire client cannot mint one: the server strips
private fields from incoming messages.

If the front desk is rebound, the old undelivered reminder is retired and the
pending episode goes to the new generation at once. With no front desk bound,
the episode stays pending and the first bound tick sends it. While the
recipient's pane is not yet verified live, delivery is deferred without
spending a transport attempt. A reminder refused because the binding was
missing or the check was off is sent again as soon as that clears. A reminder
still unsettled after two hours is replaced, so one the outbox cannot settle
does not silence the check. A reminder that a delivery currently holds is
never retired or replaced.

Delivery never changes the verified time, the blocked clock or the queue
evidence. State and reminder identity survive a daemon restart; nothing resets
to startup time.

## Storage

`v2_external_work_state` holds one row per watch. `v2_external_work_records`
holds one row per accepted record. Neither is pruned.
