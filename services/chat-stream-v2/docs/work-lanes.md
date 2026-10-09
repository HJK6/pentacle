# Work lanes wire contract (v1)

Spec: `spec_pentacle__first_class_work_lanes_2026_10` (D6, D7). Fixture: `pentacle-chat-core/tests/fixtures/work-lanes-inventory.json` (`schema: work_lanes_inventory_v1`). The daemon projection test and both chat-core TypeScript suites assert against that file; it is the contract.

## Capability

Clients send `capabilities.work_lanes_v1: true` in `hello`. Only such non-scoped clients receive lane data. Scoped (Cosmo) clients never do.

## Inventory

- Push frame `{"type":"work_lanes.inventory","lanes":[…],"counts":{"open","active","paused","blocked"},"truncated":bool,"generated_at"}`. It is signature-deduped and re-sent on any lane write and on every session-inventory recompute.
- The `hello` and `list_sessions` replies carry the same frame object as `work_lanes`.
- `lanes[]` is in server order: `blocked`, then `active`, then `paused`; within a group by `updated_at` desc, then `lane_id`. Clients must not reorder it.
- `lanes[]` never contains `done` lanes. It holds at most 64 lanes and sets `truncated:true` when more exist.
- The header count is `counts.open` (active + paused + blocked). Never derive it from sessions.
- `state` is the presented state. A stored `active` lane whose lead no longer qualifies presents `paused` with `state_reason:"lead_lost_unreconciled"`. Once the reconciler pass runs, it is stored as `paused`/`lead_lost`.
- `lead` is null or `{stream_id, generation, qualifies, status, visibility, presence{online,working,capture_liveness,last_activity}, status_card{goal,active_step,update,eta_at,eta_set_at,updated_at}, eta_stale}`. When `eta_stale` is true, render "ETA stale" instead of `late Xm`.
- `visible_chat` is `{stream_id, generation|null, kind: composite|session, available: open|history|unavailable}`.
- `last_update` is `{update_id, kind, event_id, ts}` or null.

## List RPC

Request: `{"type":"work_lanes.list","include_done":bool,"limit":<=200,"before_updated_at":<iso>|null,"before_lane_id":<id>|null}`.

Reply: `{"type":"work_lanes.list.ok","include_done","lanes":[…same lane shape…],"next_before_updated_at","next_before_lane_id"}`.

Paging uses the compound key (`updated_at`, `lane_id`), descending, so lanes with the same `updated_at` are never dropped. To fetch the next page, pass both `next_before_*` values back as `before_updated_at` and `before_lane_id`. Both are null on the last page. Within a page, lanes come in server order.

## Tap target (per `visible_chat.available`)

- `open`: open the chat. For a session, use `stream_id`; for the composite, open Bart's chat.
- `history`: show a read-only transcript. Fetch it with `{"type":"request_stream_events","stream_id":…,"generation":…,…}` using the existing limits and paging. Scoped clients stay confined to their scope stream. The lane exception admits an operator-authenticated, non-scoped client when `(stream_id, generation)` is the visible chat of a first-class lane and the pointer still validates. Note that the inherited `request_stream_events` gate already serves a stream that is absent from the open inventory to non-scoped clients, so the lane check is not the only path to a closed tail.
- The projection re-applies the pointer validator on every read. A visible chat that has since become hidden, protected, composite or a direct-primary binding presents `available: "unavailable"`.
- `unavailable`: render an explicit "Chat unavailable" state and keep the lane visible. Never fall back to Bart, a hidden worker or a new generation.

## Lane updates

Each update is a composite `chat.event` with `publish_kind:"lane_update"` and `message_id:"publication:"+update_id`, where `update_id = "lane-update:<lane_id>:<source_id>"`. `text` is the summary, so older clients render it as prose.

`raw.lane_update` is `{update_id, lane_id, kind, summary, source{type, id, grouped_ids?}, state, prior_state, owner_kind, title, ts}`, where:

- `kind` is one of `major_decision`, `lane_started`, `lane_completed`, `lane_blocked`, `lane_unblocked`, `milestone`.
- `source.type` is one of `transition`, `decision`, `milestone`, `adoption`.

Updates arrive both live (as a `chat.event` broadcast) and through history (`request_stream_events` on the composite). Deduplicate by `message_id`.

## Daemon model (for operators of the store)

- Lanes are rows of `v2_assistant_composite_lanes` with `work_state` set. All product columns are nullable, so routing-only lanes are unchanged. Product and routing operations share `version`.
- `v2_work_lane_events` is the audit and update linkage. `event_id` is the request id and `UNIQUE(lane_id, source_id)` deduplicates updates. The partial UNIQUE index on `consumed_question_id` makes each operator confirmation single-use.
- Each `assistant.operation` with `operation: work_lane.<adopt|set_state|set_lead|set_chat|set_text|set_owner|update>` and `dispatch_id: "none"` writes the lane row, the event row and the `lane_update` publication in one `BEGIN IMMEDIATE` transaction. Only the current direct-primary binding (stream + generation) may submit one; other actors get `work_lane_actor_unverified`.
- Lead loss: a coalesced refresh runs on every session-inventory recompute and every 60 s. It stores `active → paused (lead_lost)` once, and lead loss never posts an update. A handoff moves the lead (and a visible chat that pointed at the predecessor) to the successor before the predecessor closes, with one `lead_handoff` event and no state change.
- Operator lanes (`owner_kind=operator`) need a confirmation for `set_state → done`, `set_owner → fd`, routing `lane.close` and routing `lane.decision` with `transition=cancel`. The confirmation is the id of a question that:
  - was asked with `agent-orch work-lane request-confirmation <lane> --action <set_state:done|set_owner:fd|lane.close|lane.decision:cancel>`, which stores `context = {schema: WorkLaneConfirmationV1, lane_id, action}`;
  - was produced by the FD seat;
  - was answered `Confirm` by a direct operator (an agent-relayed answer does not count);
  - has not been consumed before.

## CLI

`agent-orch work-lane list [--include-done] [--json]`, `show <lane_id> [--json]`, `adopt --preview | --apply <json-file>`, and the FD-only commands `set-state | set-lead | set-chat | set-text | set-owner | update <lane_id> --expected-version N --request-id <stable id>`, plus `request-confirmation`. Retry with the same `--request-id`: a replay returns `duplicate:true` and has no second effect. `adopt --apply` uses `request_id = "adopt:" + adoption_key`, and each entry must carry an explicit `owner_kind`. `owner_kind: fd` needs FD lineage evidence; when lineage is unclear, adopt the lane as `operator`. Preview keys are `stream:<stream_id>` for open seats and `request:<request_message_id>:<lane_id>` for routing lanes (one operator request may admit several lanes, so the lane id keeps each key unique). Preview titles are derived within the 120-char bound: the whole subject when it fits, else its first sentence, else a word-boundary cut with an ellipsis; the full subject stays in `summary`.

## File-derived work (increment 1)

The `work_lanes_v1` capability carries additive member facts; the shared golden
fixture has version 2. Existing keys, lane order, counts, authentication,
lead-loss rules and status cards keep their prior behavior. A paused lane with
no lead reads the same work facts as an active lane.

The daemon reuses the shared specs subsystem and its watcher/debounce.
`PENTACLE_MEMORY_ROOT` names the root containing `work/<status>/<item>/spec.md`
and `summary.md`. A bounded sweep reads those files at that depth, excluding
artifact directories, conflict copies and symlinks. `WORK_INDEX_SWEEP_S`
defaults to 300 seconds. The existing inventory loop owns the sweep; there is
no new service, database file, catalog publisher or agent duty.

Members resolve by declared YAML `id`, including after folder moves; the
physical directory supplies status. The shared loader handles quoted and
multiline YAML. Acceptance counts include only boxes in `## Acceptance Criteria`,
ignore fenced examples and count `(waived: reason)` as checked. Estimates parse
positive `elapsed_delivery_h: 2–4 (median 3)` ranges in `## Estimate`, retaining
`provisional`. Missing fields are null. Summary `**Status** — ...` and
`**Next action** — ...` text is limited to 280 characters. Public golden inputs
are invented examples.

Each member has `spec_id`, `title`, `status`, `terminal`, `ac_checked`, `ac_total`,
`estimate {p25,p75,median,provisional}` or null, `status_text`, `next_action_text`,
`source_changed_at`, `observation {quality,observed_at,error}` and `obs_rev`.
`terminal` is `completed`, `deprecated` or null. Quality is `fresh`, `stale`,
`error`, `missing` or `ambiguous`; it does not describe lane activity.
Transient loss or split writes retain last-good facts as stale. Malformed YAML
or read errors retain them as error. Missing files and duplicates converge
after `WORK_INDEX_SETTLE_S` (default 300 seconds) and at least two periodic
sweeps; callbacks cannot accelerate the sweep count. Coherent reads restore
fresh facts. Root loss retains the durable snapshot with index availability false.

Lane additions are `members` (first 8, in membership order), `members_total`,
`no_spec_reason`, `items_total`, `items_completed`, `items_dropped`, `items_open`,
`items_unresolved`, `ac_checked`, `ac_total`, `ac_members`, `open_estimate_h`,
`open_estimated`, `estimate_complete` and `freshness_at`. Item counts form a
partition. AC totals sum resolved members with AC data; `ac_members` exposes
coverage and totals are null without coverage. Estimates sum only estimated
open members, with null when none are estimated. `estimate_complete` means
every open member has an estimate. `freshness_at` uses source changes and lane
operations, never sweep time. It is not a forecast.

Inventory and list replies add `work_index {available,root_configured,
snapshot_at,last_sweep_at,error}`. `show --members` returns all members (at most
32) beside existing events and updates, in that same shape and order without
a cursor. Increment 1 omits `completion_pending`, `lead_reported_done` and
`stale`; these arrive together with their semantics and delivery in increment 2.
No completion is inferred from report prose.

### Membership and persistence

Use `work-lane set-members <lane> --member spec_demo__bridge [--member ...]
--expected-version N --request-id ID` for ordered full replacement. Only the
authenticated current FD binding can write it. IDs are canonical, unique
`spec_...` values; 1–32 members are allowed. Empty membership requires
`--no-spec-reason` (at most 280 characters). Unknown IDs appear missing.
New adoption supplies members or a reason; legacy lanes retain their lifecycle
behavior. `adopt --preview --epic <id>` expands catalog members once, filters
to specs and deduplicates. The FD edits and confirms the list. Qualified lead
spec suggestions appear only in preview with provenance. Preview writes nothing.

Membership retains request-keyed receipts, digest conflicts and replay-before-CAS.
An identical new request is refused as `work_lane_members_unchanged`; an exact
retry of the original request replays. Title writes reject the narrow identifier
pattern in `work_lane_members.py`; unrelated writes on legacy titles are allowed.

Last-good observations and quality/settle state live in the existing Store's
`v2_work_item_observations` table. `v2_work_index_state` holds one metadata row.
Both use the same SQLite file and single writer as lanes. Separate observation
rows keep file-derived writes from incrementing the shared lifecycle version
or overwriting concurrent membership. The transaction re-reads lane membership.

`obs_rev` starts at 1 and advances only for material fresh changes in status,
AC counts, estimate, status text or next action. Snapshots and holding lanes'
`item_change` events commit atomically. IDs are
`item:<lane_id>:<spec_id>:<obs_rev>`; baseline observations emit nothing.
A→B→A→B yields revisions 2, 3, 4; restart, stale reads and repeated sweeps emit
nothing. Item changes remain lane history and never publish chat updates.

### Release preparation and rollback

Migration extends the event CHECK with `set_members` and `item_change`,
preserving rows, request receipts, indexes and publication references. The
rollback tool runs against the current owned database in a stopped-daemon
window: archive the two new event kinds, then restore the previous CHECK while
preserving all other current rows and indexes. Additive member/snapshot data
remain for a later upgrade. Never restore an old whole-database image over
concurrent data. The FD retains an independent SQLite preimage and previous
source/config/PID receipt before deployment.

Forward migration runs through the existing Store schema initialization; no
numbered migration or second database owner is introduced. Rehearse the
read-only reverse plan with `python3 services/chat-stream-v2/tools/rollback_work_lane_progress.py
--db /path/to/owned-copy.db`. In the approved stopped-daemon window, add
`--apply --confirm-offline` against the current database after retaining its
backup. Re-upgrade restores archived membership/item receipts and refuses a
conflicting receipt rather than overwriting it.

At release the FD refreshes the census, reviews the title/member/estimate
manifest, applies CAS mutations and reads each open lane back. Source validation
uses disposable synthetic memory and a file database. Production acceptance
separately binds the candidate/live PID to configured roots and proves a paused
leadless lane changes within one sweep using an owned temporary item, then
cleans it up. Never use a real item's checkbox as a fixture. A source or fixture
pass does not establish deployment.
