# Pentacle Mobile Handoff

This document is the current handoff for building a mobile client on top of Pentacle's live chat/session stack.

## Goal

Build a mobile app that can:
- list live Pentacle sessions across machines
- open a structured live chat view for Claude and Codex sessions
- stream transcript updates in real time
- show draft / queued / working state
- send messages into the live tmux-backed session

The desktop app remains the current reference implementation. The mobile app should reuse the same data plane rather than re-deriving transcript state from terminal buffers.

## Current Architecture

Pentacle currently has two relevant layers:

1. Desktop control plane in this repo
   - `main.js` starts Electron, the Python session server, and the websocket chat stream daemon
   - `preload.js` exposes IPC bridges to the renderer
   - `renderer/app.js` renders slot UI, terminal mode, and chat mode

2. Chat streaming data plane
   - Source repo: `~/agent-workspace/multi-machine-chat`
   - Daemon: `chat_streamd.py`
   - Transport: websocket on `ws://127.0.0.1:7791`
   - Source of truth: live tmux sessions on Bart, Merlin, and Amaterasu

The mobile app should consume the websocket-backed structured stream, not scrape PTY output directly.

## Session Model

Each streamed event is normalized around:
- `host`
- `provider` (`claude` or `codex`)
- `session_name`
- `stream_id`
- `timestamp`
- `kind`
- `text`

`chat_streamd.py` currently emits:
- transcript events through `chat.event`
- snapshot bootstrap through `snapshot`
- draft state as `kind = "DRAFT"`

Draft payloads also carry status in `raw`:
- `raw.draft`
- `raw.pending`
- `raw.working`

Those fields are the intended foundation for mobile UI state like:
- composer prefill
- queued message pill
- working indicator

## Machines

Current target machines:
- `bart`
- `merlin`
- `amaterasu`

Current requirement:
- both Claude and Codex must work
- mobile should not be Claude-only

Do not assume one provider or one host.

## Desktop UI State Today

Desktop slot chat mode already has:
- websocket-backed transcript rendering
- per-session filters for tools/system/code ops
- remote draft mirrored into the composer
- terminal/chat toggle at the slot level

The desktop UI is still being refined. Treat the websocket state shape as more stable than the current renderer presentation.

## Important Constraints

- tmux session remains the source of truth
- structured transcript is derived server-side from live sessions
- terminal mode remains the raw fallback
- code-edit detail should be filterable out by default in higher-level chat UIs
- old sessions may still contain imperfect normalization in edge cases; mobile should tolerate noisy text gracefully

## Mobile MVP Recommendation

1. Session list
   - grouped by host
   - provider badge
   - activity state

2. Session detail
   - unified transcript view
   - only local user sends visually emphasized
   - tool/system/code-op filtering
   - draft/queued/working indicators

3. Composer
   - show remote draft when present
   - sending should target the same tmux-backed session

4. Fallback
   - expose a "view raw terminal" affordance later, not required for first mobile pass

## Recommended Backend Direction

Do not build mobile by embedding Electron assumptions.

Instead:
- keep `chat_streamd.py` as the canonical streaming daemon
- formalize a small session API around it if needed:
  - session discovery
  - stream subscribe
  - send message
  - optional resume/attach metadata

If a mobile-specific backend is introduced, it should wrap the current daemon and tmux/session controls rather than duplicating transcript extraction logic.

## Files To Read First

- `README.md`
- `CLAUDE.md`
- `renderer/app.js`
- `preload.js`
- `main.js`
- `~/agent-workspace/multi-machine-chat/chat_streamd.py`
- `~/agent-workspace/multi-machine-chat/session.py`
- `~/agent-workspace/multi-machine-chat/codex_provider.py`

## Current Status Snapshot

As of 2026-04-24:
- desktop Pentacle has a websocket-backed chat mode
- Bart, Merlin, and Amaterasu are wired into the daemon
- Claude and Codex both stream through the same normalized UI path
- footer/status noise like `gpt-5.4 default ...` was explicitly filtered from chat transcript parsing
- draft state is separated from committed transcript events

## Open Work

- improve visual treatment of working / queued / draft state
- harden normalization around provider-specific edge cases
- define a cleaner public API contract for non-Electron clients
- build the actual mobile client on top of the websocket/session layer

## What A New Agent Should Do

1. Verify current websocket snapshot shape from `chat_streamd.py`
2. Confirm session send-path for mobile entry
3. Write a thin transport spec for mobile
4. Build mobile UI against the transport, not against renderer internals
