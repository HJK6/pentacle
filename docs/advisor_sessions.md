# Short advisor sessions

The default remains a bound advisor seat. With `session_kinds` empty, existing
ruling behavior is unchanged. Each enabled action instead gets a fresh hidden
advisor session using the configured provider, model, effort and account.

## Commands

Any seat with a verified stream token can submit asynchronous advice:

```sh
agent-orch assistant advise --work-item spec_example__task --body-file /tmp/gate.txt --request-id gate-1
# Use --work-item none for a request without a work item.
agent-orch assistant answer --ruling-request-id ruling-ID --request-id answer-1 --body-file /tmp/answer.txt
```

`advise` immediately returns a ruling request id. The same requester/request id
is idempotent; changed content is refused. Only the assigned stream and generation
can answer, with at most 6,000 UTF-8 bytes. The answer and one `[Advisor answer]`
notice are recorded atomically; the notice wakes the requesting front desk.
Advice is never released or expired unruled. A disabled authority refuses new
advice. Other actions still use `assistant ruling`; it refuses advice rows.

Only the existing authority configuration actor can change session settings:

```sh
agent-orch assistant authority
agent-orch assistant authority --session-kinds spawn,session_close
agent-orch assistant authority --session-kinds spawn,session_close,question,send --account account-alias
```

`assistant.authority.session_kinds` holds the action list. Every action means
`spawn,send,session_close,question,composite_admit,composite_close,advice`.
`assistant.authority.session_shape` holds provider/model/effort/account, seeded
once from the bound advisor's recorded shape. Rebinding and dead-seat recording
do not overwrite it. `--account` changes its account. The readout includes both
settings and running/waiting counts.

Adding an action leaves requests already at the bound seat there. Removing an
action requires a ready bound seat, closes its pending sessions, and reissues
both covered and non-covered session requests once to that seat's generation.
Requests already waiting at the bound seat retain their original packet.
After enabling every action, `--set sessions` unbinds the long-lived advisor and
returns its pending requests to the session queue. This binding bypasses no
request, emits no authority-loss notice and starts no replacement long-lived
advisor. Rollback:

```sh
# From the sessions binding, first start and bind a ready advisor:
agent-orch assistant authority --set host:advisor --brief-file /tmp/advisor-brief.txt
agent-orch assistant authority --session-kinds none
```

## Lifecycle and records

`v2_advisor_sessions` records request id, attempt, stream/generation, guidelines
identity, start/ready/end times, end reason and frozen spawn payload. The matching
unended record establishes session ownership. Ruling rows also record `work_item`
and the advice `answer`.

The daemon uses its service spawn path, no role, the default seat folder, the
front desk as parent and no parent watch. One initial prompt contains session
rules, current advisor guidelines, thread record and request packet. No second
request or overdue notice reaches that advisor. The thread has at most eight
terminal requests for the same nonempty work item, oldest first, totaling at most
12,000 UTF-8 bytes; each packet and advice answer is cut to 1,500 bytes.

Requests for one work item run in order, including earlier bound-seat requests
and approved actions still releasing. Different work items can run in parallel,
at most four sessions total. Waiting non-covered requests (close and composite
actions) take a free slot before covered requests.

Terminal outcomes close their sessions. Spawn refusal, seat loss, or no answer
within 300 seconds after readiness fails an attempt. Covered requests get one
immediate replacement; after two failures they stay pending, notify requester
and front desk once, and retry every 900 seconds. Non-covered requests get one
replacement only with at least 300 seconds left before their deadline; otherwise
they use existing `authority_unavailable` expiry/release. Their deadline applies
while queued or running.

Account controls protect owning sessions. A recent advice session can supply a
`DELEGATED-HOLD` for its requester or front desk under existing dispatch/tell
checks. Host transfer omits the `sessions` sentinel from the roster, closes
sessions after claim, and restarts queued reviews after commit publication.
Cleanup of predecessor seats may finish later without blocking new reviews.

## Measurement

Run this on a read-only reporting snapshot, binding `:stage_one_started` to the
rollout's Unix timestamp. It counts all attempts for reviewed requests and avoids
duplicating responses across cumulative usage records:

```sql
WITH reviewed AS (
  SELECT DISTINCT r.ruling_request_id, r.action, r.ruling, r.state
  FROM v2_assistant_lane_rulings r JOIN v2_advisor_sessions s USING (ruling_request_id)
  WHERE s.started_at >= :stage_one_started
    AND (r.ruling IS NOT NULL OR r.state = 'answered')
), native AS (
  SELECT DISTINCT s.ruling_request_id, u.host, u.native_session_id
  FROM v2_advisor_sessions s JOIN reviewed r USING (ruling_request_id)
  JOIN v2_usage_records u ON u.stream_id = s.stream_id AND u.generation = s.generation
    AND u.provider = 'codex'
), costs AS (
  SELECT n.ruling_request_id, count(*) AS responses,
    sum(c.input - c.cached_input + 0.1 * c.cached_input + 8 * c.output) AS cost_units
  FROM native n JOIN v2_usage_codex_responses c
    ON c.host = n.host AND c.native_session_id = n.native_session_id
  GROUP BY n.ruling_request_id
)
SELECT r.*, c.responses, c.cost_units
FROM reviewed r LEFT JOIN costs c USING (ruling_request_id);
```

Missing usage is not zero cost. Reconcile every attempt's usage coverage before
averaging. Record request count, mean cost, ruling shares by action, model/effort,
and incorrect or overruled answers. Raw token counts alone do not track quota.
