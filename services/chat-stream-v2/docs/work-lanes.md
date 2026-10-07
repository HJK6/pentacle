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
