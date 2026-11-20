# Lead disposition and close

An open, live lead or nexus attends its assigned item and any item explicitly
granted at spawn or attached by the operator. The current front desk attends
its explicit working assignment. Parentage, inherited tags and planning or
review roles alone confer no implementation attendance. The daemon uses these
facts for front desk commissions, voluntary departures and `work unattended`.

An ordinary close, including an approved manager close, checks the target
generation and every item that would lose attendance. A final live owner of
fresh `in_progress` work receives `work_last_owner` before termination. The
same check runs when an assignment change, detachment or demotion removes the
last owner. The response includes each item, the expected seat generation and
commands to assign a live successor or pause execution with the observed
publisher revisions. Choose the disposition that describes the actual work.
After an approved close is refused, retry the original intent with the same
request ID to reuse its ruling.

Missing reports and planner obligations remain warnings. Uncertain source or
seat observations admit the action and retain a cause for recovery. Confirmed
death and offline retirement keep custody. Query `work unattended --json` for
fresh in-progress items lacking an owner. Unverifiable items appear in the
omitted population. The latest fallback observation per item and gate lives in
`v2_work_attendance_causes` with its stream, generation, role and timestamp.

Each lead, nexus or planner assignment starts a disposition episode in the work
index. An ended episode retains unresolved debt for that generation and its
parent or current front desk. A fresh settled disposition or a live effective
successor clears debt. A subsequent active revision invalidates an earlier
disposition.

`agent-orch work owed --json` reads the persisted population. The lane digest
includes totals, per-repo counts, the oldest eight entries, unknown count and
query command. Nonempty debt recurs after prior delivery. Pending digests keep
their retries. A fresh empty population returns to change-only delivery after
one clearing update. Child report-ready notices include
`owes_disposition_on_close`.
