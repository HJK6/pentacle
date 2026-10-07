# Work lanes in the web renderer

The web/desktop renderer shows the daemon's first-class work lanes. The daemon owns lane identity, presented state (`active`, `paused`, `blocked`), count, order and tap target; the renderer only presents them. The wire contract and the shared fixture are in [work-lanes.md](../services/chat-stream-v2/docs/work-lanes.md) and `pentacle-chat-core/tests/fixtures/work-lanes-inventory.json`.

## What the operator sees

- **Header.** `#stats` leads with the open-lane count: `4 lanes | 6 sessions | 1 need answer | 2 working`. The lane number is the daemon's `counts.open` (active + paused + blocked), never a session count. Until a lane-aware daemon sends its first inventory, the line is the previous session-only line. `done` lanes are never counted or listed.
- **Lanes (N) section.** Above the session tiers, in the daemon's order (blocked, active, paused; the renderer never reorders). Each row shows the lane title, an owner glyph (◆ operator-started, ◇ FD-managed), the state badge, a presence dot for the linked lead (working, idle, offline, or dashed when no visible lead), the blocker for a blocked lane, the lead's status card, and the ETA. A stale ETA reads `ETA stale`, never `late Xm`.
- **Tap.** The row opens the lane's `visible_chat` according to `available`:
  - `open`: the chat opens in a slot through the same path as a sidebar row (a session, or the composite). A chat that is not in this window's roster shows a toast; nothing else opens.
  - `history`: a read-only slot for the closed chat. The transcript is read with `request_stream_events {stream_id, generation}` using the lane's generation, shown under a "Read-only history" banner, with no composer, no send and no terminal. The slot is named `lane-history:<lane_id>`, so an open session of the same name (a newer generation) is never mistaken for it. A failed or empty read says so ("Messages could not be loaded." / "No retained messages.").
  - `unavailable`: a slot with an explicit "Chat unavailable" card. No chat is read or opened, and the lane stays in the panel.
- **Ordering.** Lane pushes and the `work_lanes` field of a snapshot go through the same connection state-version gate as the rest of the daemon state, so a stale frame never rolls the panel back. The lane generation is passed only by the lane-history slot's own reads (initial, paging, retry, reconnect refetch); another slot reading the same stream id never sends it.
- **Lane update cards.** Bart's chat renders a typed card for every `publish_kind: "lane_update"` publication (`lane_started`, `lane_completed`, `lane_blocked`, `lane_unblocked`, `major_decision`, `milestone`) from `raw.lane_update`. A client that does not branch on the kind shows the summary as ordinary assistant text.

## Where it lives

| Concern | File |
|---|---|
| Wire validation, selectors, tap target, ETA label, `lane_update` parsing (named slice "work_lanes projection v1") | `pentacle-chat-core/src/services/workLanes.ts` |
| Transcript row carries `laneUpdate` | `pentacle-chat-core/src/services/pentacleChatModel.ts` |
| Stats line and panel markup | `renderer/work_lanes_panel.js` |
| Frame ingestion, tap routing, history slot | `renderer/app.js` (`applyWorkLanesPayload`, `openWorkLane`, `openLaneHistorySlot`, `loadLaneHistory`) |
| `lane_update` card | `renderer/src/shared_transcript_view.ts` |
| `work_lanes_v1` capability, snapshot cache, `generation` on history reads | `main/chat_stream_client.js` |
| Generation on paged history | `renderer/chat_events_lazy.js` |

## Validation

`pentacle-chat-core/tests/workLanes.test.ts` (fixture parity), `test/work_lanes_panel.test.ts`, `test/work_lanes_transcript.test.ts`, `test/work_lanes_client.test.js`, `test/work_lanes_web.test.js` (real `renderer/app.js` in jsdom), and the `web-work-lanes` scenario of `node test/e2e/web_gate.js` (real Chrome and stylesheet; the lane frames are the frozen fixture injected through the renderer's frame listener, because the seeded gate daemon owns no lanes or closed chats).
