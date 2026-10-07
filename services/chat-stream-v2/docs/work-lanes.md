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

Request: `{"type":"work_lanes.list","include_done":bool,"limit":<=200,"before_updated_at":<iso>|null}`.

Reply: `{"type":"work_lanes.list.ok","include_done","lanes":[…same lane shape…],"next_before_updated_at"}`.

## Tap target (per `visible_chat.available`)

- `open`: open the chat. For a session, use `stream_id`; for the composite, open Bart's chat.
- `history`: show a read-only transcript. Fetch it with `{"type":"request_stream_events","stream_id":…,"generation":…,…}` using the existing limits and paging. The daemon serves a closed stream only when `(stream_id, generation)` is the visible chat of a first-class lane and the pointer still validates. It refuses scoped clients and hidden or backend seats.
- `unavailable`: render an explicit "Chat unavailable" state and keep the lane visible. Never fall back to Bart, a hidden worker or a new generation.

## Lane updates

Each update is a composite `chat.event` with `publish_kind:"lane_update"` and `message_id:"publication:"+update_id`, where `update_id = "lane-update:<lane_id>:<source_id>"`. `text` is the summary, so older clients render it as prose.

`raw.lane_update` is `{update_id, lane_id, kind, summary, source{type, id, grouped_ids?}, state, prior_state, owner_kind, title, ts}`, where:

- `kind` is one of `major_decision`, `lane_started`, `lane_completed`, `lane_blocked`, `lane_unblocked`, `milestone`.
- `source.type` is one of `transition`, `decision`, `milestone`, `adoption`.

Updates arrive both live (as a `chat.event` broadcast) and through history (`request_stream_events` on the composite). Deduplicate by `message_id`.
