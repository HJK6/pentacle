# Assistant activity projection

The assistant-composite activity snapshot is a read-only, per-input view over
the existing route, publication, lane, and operation receipts. It does not add
a table or service. The routing store may add `route_json.timings` keys, using
`setdefault` so a replay never replaces an accepted timestamp.

Each input reports an independent `response_state` and `work_state`:

- Response states are `queued`, `routing`, `awaiting_reply`, `acknowledged`,
  `answered`, `reply_received`, `waiting_for_operator`,
  `waiting_for_dependency`, `failed`, `uncertain`, or `cancelled`.
- Work states are `unknown`, `discussion`, `in_progress`, `waiting`,
  `outcome_reported`, `closed`, or `cancelled`.
- A final/result publication closes the response obligation only. Missing lane
  receipts remain `unknown`; a response never asserts that project work is
  complete.
- Failed routes, uncertain delivery, and cancelled lane sets take precedence
  over a contradictory final publication. Prose alone is `reply_received`.

The timing fields are derived from durable UTC receipts: `accepted_at` is route
creation, `first_visible_at` is the first publication, and
`final_visible_at` is the first final/result publication. `queue_latency_ms`
and `routing_latency_ms` use `routing_started_at` and `routed_at`; missing,
invalid, or negative intervals are `null`.

The desktop's `STALE_TURN_GRACE_MS` quiet heuristic is for ordinary agent
streams only. An `assistant_composite` stream never arms that timer and the
timer cannot clear its underlying working turn. `getTurnPhase()` may report
`idle` for composite send eligibility; this does not mean the activity
projection was completed by quiet time.

The CLI marker seam is explicit and typed:
`agent-orch assistant publish --response-state acknowledged` or `final` adds
the marker to the immutable publication payload. The projection treats these
markers as durable evidence and preserves them through live/history replay.
