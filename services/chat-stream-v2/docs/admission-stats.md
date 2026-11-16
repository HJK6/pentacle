# Current admission holders

`daemon.stats` includes `stats.admission` schema 1. This is a read-only,
point-in-time snapshot, collected synchronously with one monotonic timestamp.
Constant `subsystem` and `bug_ref` tags bind diagnostic readbacks to this repair.
Use `tools/read_daemon_stats.py` on the daemon host to read it without a browser.
Existing dispatch authentication and restricted-client allow-lists still apply.

The snapshot contains:

- `connections`: current accepted transport count and connection limit.
- `pools`: ordinary, parked, durable, control and diag entries.
- Each pool's `usage`: admitted task count and retained raw request bytes.
- `limits`: task/byte ceilings for global, connection and principal axes.
- `held_verbs`: registered verb buckets, each with `count` and
  `oldest_age_ms`. Unrecognized or oversized verb labels become `unknown`.
- `connections`: nonzero holders, with random `connection_id`, `active`,
  opaque `principal_ids`, task/byte usage and held verbs.
- `principals`: nonzero holders, with opaque `principal_id`, fixed `kind`,
  connection ids, task/byte usage and held verbs.

`active` means the originating WebSocket is still registered in the server.
A detached durable request can keep its original connection id and credit after
that transport disconnects, with `active=false`, until the task finishes.
Connection ids match `conn_diag` ids. Principal ids are keyed digests stable
only within one daemon boot and correlate the same principal across sockets and
pools. No credential ids, stream ids, service names, addresses, client-provided
names, request ids, request content, tokens or blob bytes are exposed.

Only admitted holders are retained. Completion, cancellation and disconnect
cleanup remove their records through the existing credit-release callback.
Principal transfer moves attribution with the actual credit. There is no
history, high-water retention, background sampler or new diagnostic task.
Idle connections/principals have no holder rows. Empty snapshots cannot prove
which client or verb held capacity at an earlier refusal.

## Diagnostic capacity

Established clients' `daemon.stats` uses a dedicated `diag` pool: one task
fleet-wide, one per connection/principal, and 16 KiB raw request bytes on each
axis. These diagnostic caps are fixed; `PENTACLE_ADMIT_DIAG` does not raise them.
No other verb uses this pool. Existing ordinary/parked/durable/control limits
and their behavior are unchanged. A snapshot includes its own admitted stats
credit in `diag`, so that count normally equals one over the wire.

The single diagnostic slot is shared by every eligible client. An overlapping
stats call returns retryable `server_overloaded`. It never occupies ping's
control capacity. Remote preauthentication stats requests stay in ordinary
admission and do not bypass existing authorization. A newly connecting remote
client may still be unable to complete ordinary hello while ordinary capacity
is exhausted; the existing loopback reader can diagnose without hello.

## Interpreting a refusal

Compare each pool's current task/byte usage to its global, connection and
principal ceilings, then inspect the corresponding holder's verb counts and
oldest ages. Web tabs currently share the web host's single daemon connection;
a fresh browser profile does not provide separate daemon connection credits.
The metadata makes current holders observable. It does not identify a historical
limiting pool, prove why a handler is held, change the shared web credit model,
raise any existing limit, or repair the held operation. Investigate the actual
holder before choosing a repair. If the condition is absent at the installed
readback, record that limit and return to the release owner.
