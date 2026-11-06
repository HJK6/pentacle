# Lead disposition and close

An effective assigned lead, nexus or planner owns its task, including hidden
seats. Voluntary close of a sole owner requires a fresh accepted non-active
spec status or another live same-spec owner. `analysis` and `in_progress` are
active; `backlog`, `ready_for_dev`, `blocked`, `needs_qa`, `completed` and
`deprecated` are dispositions. No additional move is required for work already
settled in one of those statuses.

The existing close guard waits for one work-index refresh, bounded to thirty
seconds, then returns `lead_close_requires_disposition` while active or unknown
work remains. Missing, stale, ambiguous, unavailable or pending observations
refuse before termination. The reply names the spec, observed status/time and
publisher commands with the observed document revisions. A publisher timeout or
uncertain same-ID request grants no close. The target generation is pinned
before the wait. The graph lock is released during refresh and held for the
final ownership/successor check through termination; assignment and role changes
use the same fence.

Publish a legitimate disposition with reason, next action and requesting-stream
attribution, or use `agent-orch spec assign <successor> <spec>`. The daemon must
observe the result. It never moves a spec automatically. A successor is an open,
non-presumed-dead, non-protected, non-composite lead/nexus/planner effectively
assigned to the same spec. Authority tags, QA/worker roles, reports and prose
custody do not qualify. Closing one parallel owner leaves the others tracked.

Ordinary parent/FD `--operator-confirm`, approved close rulings and done or
aborted reports do not waive disposition. The configured FD uses this guard
even for its own children. An explicit FD `--force` request with a reason and
request id may recover its own freshly proven busy child, preserving disposition
debt with FD custody. It cannot recover another parent's child this way; idle or
unknown capture does not waive disposition. The current FD binding, target
generation, parentage and absence of live children/pending spawns are rechecked
at final admission. Elevated seat credentials remain seat
credentials even when marked `operator_authenticated`. Genuine connection-bound
operator recovery, confirmed gone manager panes, existing offline/deferred
retirement and failed-spawn cleanup keep their recovery paths and durable owed
custody. Uncertain liveness does not prove death. Existing assistant protection,
generation, child-preservation and local/remote checks still apply.

Each assignment starts an episode in the existing work-index state JSON.
Unassignment, detachment, reassignment or a role change retains unresolved debt
for that stream and generation; ordinary close checks current assignments and
that history. A fresh settled disposition clears debt, including on first
discovery, and a return to an active status invalidates it. A live effective
successor also clears debt. No table, service or background timer is added.

`agent-orch work owed --json` reads the full persisted population. Ended episodes
without disposition or succession retain parent or current front-desk custody
through restart and root-generation changes. Unknown debt remains degraded.
The existing `lane_digest` includes totals, per-repo counts, the oldest eight,
unknown count and query command. Nonempty debt recurs after prior delivery;
pending digests retain their retries. Disabled delivery is exposed. A fresh
empty population returns to change-only delivery after one clearing update.
Child report-ready notices include `owes_disposition_on_close`; reporting alone
clears nothing. Rollback preserves the additive history.
