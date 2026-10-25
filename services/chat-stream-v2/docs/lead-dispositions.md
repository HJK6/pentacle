# Lead disposition and close

A current assigned lead or nexus cannot voluntarily close its sole fresh
`in_progress` assignment. The close waits for one refresh through the existing
work-index drain, bounded to thirty seconds, then returns
`lead_close_requires_disposition` if the observation is unchanged. The reply
contains the spec, observed status/time and recovery commands with the observed
document revisions. `pending: true` means the disposition remains outstanding;
an unavailable publisher or an uncertain same-ID request grants no close.

Move the item through the memory publisher with `reason`, `next_action` and
requesting-stream attribution, or assign a successor with
`agent-orch spec assign <successor> <spec>`. A move counts after the daemon
observes it. The existing authenticated operator route, or an explicit
`--operator-confirm` close by the verified direct parent, ends the assignment
and retains any owed disposition. A self-supplied confirmation, force flag or
historical move receipt cannot bypass the gate. Existing assistant protection,
generation, child-preservation and local/remote close checks still apply.

Only an open, non-presumed-dead lead/nexus with the effective assignment is a
successor. Authority tags alone, QA/planner/worker roles, closed seats and other
assignments do not qualify. Gone/offline manager closes and other involuntary
close kinds proceed through their existing lifecycle checks.
Stale or unavailable work observations allow the close and retain unknown debt.

Each effective assignment starts an episode with the current accepted
observation revision or an unknown baseline. An accepted revision transition
away from `in_progress`, or a newly changed valid blocked `parked:` declaration,
provides disposition evidence. First discovery of an unknown status, an
unchanged park, a report, delivery or an unrelated revision edit does not.
Re-entry into `in_progress` invalidates earlier evidence. Untouched backlog and
blocked items with no assignment episode owe nothing.

An ended episode without a successor or disposition creates debt against its
parent, falling back to current front-desk custody. Unknown baselines and
unusable ending observations remain conservative through restart, stale scans
and root-generation changes. A later accepted disposition or new effective
lead clears debt. Bookkeeping uses the existing work-index state JSON; it adds
no table, service or background timer. Rollback may ignore this additive
metadata without deleting it.

`agent-orch work owed --json` reads the full persisted population. The enabled
`lane_digest` includes totals, per-repo counts, the oldest eight, unknown count
and query command. Nonempty debt recurs at each deadline after the prior digest
is delivered; a pending/in-flight digest keeps its retries. Unknown debt is
degraded. Disabled delivery is exposed as disabled. An empty fresh population
returns to change-only delivery after one clearing update. Child report-ready
notices include `owes_disposition_on_close`; reporting alone clears nothing.
