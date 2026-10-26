# Front desk wake reduction

Only the primary assistant's bound direct-primary front desk uses this policy (the unprefixed `PENTACLE_ASSISTANT_*` composite, whatever you have titled it). Other parents and
router-backed assistant seats keep their existing delivery behavior.

Operator input and question answers, GATE and BLOCKER tells, child reports,
rejected or revised lane rulings, failed spawn releases, routed external email,
and the desk's timed wakes arrive immediately. So does one notice when the bound
lane-ruling authority seat is lost (closed, pane dead, or no such seat): it is
sent once per continuous loss of that seat generation, also with no request
pending, and again only after the authority was seen ready or was rebound. A
seat whose state is merely unknown, as right after a daemon start, is not
reported. The daemon does not rebind the authority; requests keep being
released without a ruling until you rebind or replace it. Pending requests for
the lost authority stream and generation expire on the next worker tick as
`authority_unavailable`, using the same release and operator mirror as a new
request made while the authority is unavailable. The chat line and the
deadline notice for a request that went unruled state what the release did:
`admitted unruled`, or `not released, blocked (<code>)` when nothing launched. The
requester's structured result notice still follows. Successful unchanged-scope spawn
approvals produce no front-desk tell. Tree-idle and child context-advisory notices
are dropped; their facts remain available through `agent-orch list` and `inspect`.
The daemon's own [external-work reminder](../services/chat-stream-v2/docs/external-work.md)
and a [lost-lead lane alert](../services/chat-stream-v2/docs/work-lanes.md#owner-loss-lead_lost-fault)
(`lane_lead_lost`) also arrive immediately; a wire client cannot mint either.

Other inputs, including START/END, receipts, concurs and watched-child inactivity,
are held in the existing durable outbound-notice store. The oldest held item's
creation time sets the deadline: `PENTACLE_FRONT_DESK_DIGEST_S` defaults to 3600
seconds. Later arrivals never reset it. At the deadline, all held items become
one `lane_digest` notice through the existing outbound queue. No empty digest is
sent. Wake inputs arrive alone and do not flush held items. The queue's normal
five-second sweep bounds scheduling precision; its existing transport and proof
recovery govern the resulting notice. Held inputs survive daemon restarts.

Delivered and persisted-suppressed START/GATE/BLOCKER/END tells from an open
lead or nexus also update the sender's status card with the first 300 characters
of the milestone body, after any peer header. Other tells leave it untouched;
card-update failures are logged without changing the tell outcome. Replaying a
tell id does not stamp the card again.

Internally queued lane digests, including fleet snapshots, bypass holding using
the existing private digest token. The token survives both notice and tell ingress
checks; a digest is never held as text for a later digest. Peer messages that copy
a digest header or token-shaped value retain ordinary admission. Existing held
payloads from an older daemon remain immutable and drain on their original
deadline; record that legacy boundary before claiming no recurring nesting.

`PENTACLE_FRONT_DESK_DIGEST_ENABLED` defaults to on. Set it to `0`, `false`,
`no`, or `off` in the daemon environment to disable ingress holding and dropping:
every new input then passes through. Existing held rows are folded into one
digest on the next outbound sweep, without waiting for the deadline. With the
switch on, a restart preserves the oldest-item deadline. Pending rows for the
same target from an earlier binding generation are included in the digest under
the current generation, so they cannot be stranded by the old generation filter.
The resulting digest uses the normal queue's delivery and recovery rules.

Canonical direct operator dispatches bypass admission using the private marker
introduced by `Comms.send_assistant_backend`, before any hold/drop decision.
The bypass applies only to marked sends; forwarded composite peer tells also
carry the marker but remain subject to admission. Wire clients cannot supply
that marker; copying the daemon dispatch header in a peer tell, addressed to
either the desk or its composite, does not make it operator input. GATE/BLOCKER classification applies
to the tell's leading label (after the notice prefix), not references to another
seat's GATE buried in a progress update.
Authenticated daily-brief service tells classify the sanitized original supplied
by the server before its provenance stamp. Queue replay requires explicit stored
service provenance; arbitrary copied peer headers and legacy rows do not gain
that exemption. See [service tells](wmi_brief_tell.md).

The front-desk Claude advisory/compact thresholds are 150K/200K tokens, configured
with `PENTACLE_CONTEXT_ASSISTANT_ADVISORY_ABS` and
`PENTACLE_CONTEXT_ASSISTANT_COMPACT_ABS`. Other Claude seats retain 400K/500K.
An unproven compact input can retry after `PENTACLE_CONTEXT_COMPACT_COOLDOWN_S`
when the seat remains over threshold, idle and has a proven-empty composer.
Late proof prevents a repaste; a real draft still defers. A successful compact
whose proof was missed can be repeated after the cooldown on an old high reading.

## Compact ruling results

The structured result retains the request id, action, target, ruling, release
state, complete reason and conditions. Its outcome carries only operational
facts: admitted stream ids/count/scope, prompt delivery state, actual close
result, and failure codes/reasons. Admission is not readiness; approval is not
closure. Session JSON, usage, observer binding and prompt staging are omitted.
Long conditions and failure reasons are not truncated. Existing queued result
payloads retain their original identity and bytes through retries.

`detail_ref` names the exact row in `v2_assistant_lane_rulings` by
`ruling_request_id`. The full outcome remains in that durable row. An authorized
reader can use the daemon's configured sessions database for an exact lookup:

```sql
SELECT action, state, ruling, reason, conditions, outcome_json
FROM v2_assistant_lane_rulings WHERE ruling_request_id = :ruling_request_id;
```

`agent-orch inspect <target>` shows only the latest ruling for that target. Check
its request id before using it as detail for a notice about an earlier ruling.

## Missing lane estimates

`lane_estimate_missing` joins the existing work-update digest as a FLAG for
freshly observed incomplete remaining-work coverage. Paused and leadless lanes
are included. One lane-owned episode survives unchanged sweeps, membership
changes that remain deficient, restart and FD handoff. Unknown observations hold
state, correction clears silently, and recurrence gets a new sequence. The
recorded opening names the lane and its current `work-lane show --members`
lookup. Pending historical openings use the existing durable delivery path.
Admission and actionable GATE/BLOCKER, operator and child-report paths retain
their behavior. See [work lanes](../services/chat-stream-v2/docs/work-lanes.md#missing-remaining-estimates-flag)
for predicate, parser and compatible rollback details.
