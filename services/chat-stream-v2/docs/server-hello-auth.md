# Hello receive time and failed-auth fan-out

## Operator challenge timing

The operator nonce expires after 10 seconds. The server compares its expiry with
the wall-clock time when the application reads the hello, before admission or
hello-ordering waits. A request-local server context carries that timestamp;
message fields cannot supply or override it. Time spent before the application
reads the frame is not covered.

An on-time hello can therefore survive an ordering wait longer than the nonce
TTL. A genuinely late arrival is refused. The nonce is still consumed by the
first authentication attempt on its socket; replay and cross-socket proofs do
not gain another attempt. Credential, proof and revocation checks still run at
authentication time. `hello.error` retains its existing `error_code` and shape.

## Rejected claimed proofs

A rejected operator proof leaves a sticky, connection-bound rejection marker.
Later hellos on that connection cannot return to native-loopback bootstrap.
`_frame_for_client` withholds all broadcast projections for the marked connection.
`broadcast()` also filters marked members before selecting a shared projection
representative, so an eligible peer cannot lend its projection to a rejected
peer and a rejected representative cannot suppress an eligible peer.
Ordinary RPC dispatch also removes the native-loopback exemption after rejection.
The queued broadcast writer checks the marker under its send lock before every
send, so a projection queued before rejection is dropped without recording it
as delivered. Direct handshake error replies remain available.

The deliberate native-loopback, no-claim bootstrap remains available. An Origin
header removes that exception, including empty and `null` Origin values. Remote
unauthenticated clients do not gain broadcast access. Existing authenticated
operator, seat and scoped projection policies remain unchanged.

## Whole-connection delivery evidence

Every `conn_diag` close record adds `non_handshake_tx`: the number of successful
frame sends other than `welcome` and `hello.error`. Direct and broadcast sends
both contribute, including frames sent before a proof rejection. Failed sends
do not contribute. Existing traffic buckets and `tx_messages` remain unchanged;
the handshake frames still belong to the `other` traffic bucket.

## Conservative liveness exclusion

`deploy.deploy.classify_slow_consumer_lines` excludes a structured keepalive or
1011 close only when the whole selected input proves all of the following for
the same nonempty `conn_id`:

- An `auth_fail` exists and no `auth_ok` exists, regardless of record order.
- Every observed close has `auth_state="failed"` and an explicit integer
  `non_handshake_tx=0`.
- Present delivery counters do not contradict that evidence, and authentication
  telemetry does not report suppressed decisions.

Missing, legacy, pending, malformed or contradictory evidence remains a failure.
In particular, a sticky rejection marker does not prove no prior access.
Booleans, floating-point values and strings are not integer counter evidence.
An overflow or peer-liveness failure is never excluded by this rule. Plaintext
failures cannot borrow evidence from a structured record with a matching label.

The result adds `excluded_auth_failure_count`, `excluded_auth_failure_conn_ids`
and `excluded_auth_failures`. The count and connection IDs cover all exclusions;
the rows honor `max_examples`. `failure_count`, `failures`, `kinds` and `outcome`
continue to describe the non-excluded failures. Authentication failures remain
diagnostics and still fail required authentication or restoration probes.

Replaying a historical window is a control, not candidate certification. A
legacy close without the new field continues to count, even if its old bucket
counters look like a handshake-only exchange.

## Focused controls

`tests/test_server_hello_receive_time.py` drives the application receive path,
including a real 11-second ordering wait and nonce/proof/revocation controls.
`tests/test_server_fanout_auth_boundary.py` uses the real application read,
authentication, broadcast and writer paths with an in-memory transport adapter.
It covers the 3-by-3 transport/hello-state matrix, both mixed-group orders and
valid operator, seat and scoped principals, plus rejected-proof RPC admission
and rejection while queued inventory waits for the writer lock. Existing browser-origin and smoke
connection-logging tests additionally exercise disposable local WebSocket
servers. No production endpoints are needed.
