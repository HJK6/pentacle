# Seat busy versus working

A seat can be waiting at an empty composer while work it owns is still running. Every open session row therefore carries two separate signals.

| Field | Meaning | Used by |
|---|---|---|
| `working` | The pane is generating: a live provider spinner is on screen. | Delivery, nudges, compaction input, close `defer_if_working`, `working.state` frames |
| `busy` | Something the seat owns is running: it is generating, the provider reports background work, or an open descendant seat is busy. | `agent-orch list`, the Lanes presence dot, the fleet lane digest, tree-idle notices |

`busy_reasons` lists why a row is busy: `generating`, `background` or `child_seat`. An open descendant that is itself idle adds nothing, and neither does a dead or offline row. `background_busy` and `background_label` (for example `1 shell · 1 subagent`) come from the pane capture.

## What counts as background work

The capture parser (`services/chat-stream-v2/mirror.py`) reads only what the provider itself shows:

- **Claude Code:** the footer lines below the composer. It counts the `N shell(s)` and `N local agent(s)` pills, and the rows of the subagent panel that appear under `● main` while a background subagent runs.
- **Codex:** `N background terminal(s) running` in the status tail.

The parser ignores footer text quoted in the transcript above the composer, and it does not count `← N agent`, `/tasks to see subagents`, monitors, teams or other pills. It does not inspect OS processes either.

The parser has no extra timer. The pills are static text, so the next capture after the work ends clears `background_busy`, and with it `busy`.

The Lanes presence dot shows `working` for a lane whose lead is busy. Lane state (active, paused, blocked, done) is never derived from `busy`.
