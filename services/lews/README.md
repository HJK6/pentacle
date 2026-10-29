# LEWS mode (daemon side)

LEWS ("Let the Engine Work, Stupid") is Pentacle Mobile's hands-free voice loop:
the phone speaks agent completions, status updates, questions and alerts, and
routes spoken requests to a concierge agent that drives the fleet through
`agent-orch`. The mobile side is documented in
`pentacle-mobile/docs/LEWS_MODE.md`.

Nothing in the daemon changes for LEWS mode. It uses existing verbs only:

| What the phone does | Verb |
| --- | --- |
| Spawn the concierge | `spawn` with `schema: SpawnRequestV2`, plus `role: "lews"` and `initial_prompt: <brief>` (the same additive fields `agent-orch spawn --role/--initial-prompt` sends) |
| Title it | `rename` with `display_name: "LEWS"` |
| Ask it something | `send` to the concierge session |
| Read its answer | `request_stream_events` on the concierge stream after `session.inventory` reports `working: false` |
| Answer an agent question card by voice | `prompt.answer` (same frame as the Updates tab) |
| Answer a provider selector by voice | `question.dismiss` + `send` (same path as the chat list) |

The concierge is an ordinary Claude session on the daemon host with the role
`lews`. Its standing brief is [LEWS_CONCIERGE.md](LEWS_CONCIERGE.md); the phone
sends the same text as the initial prompt, and the brief itself tells the
session how to use `agent-orch list`, `inspect`, `tell`, `prompt list/answer`
and `spawn`. Replies are read aloud, so the brief demands short plain prose.

## Spawning one by hand

Useful to check the brief or to pre-warm a concierge before a drive:

```sh
cd ~/pentacle
AGENT_ORCH_HOST_ID=local ./.venv/bin/agent-orch spawn --provider claude --top-level \
  --role lews --objective "LEWS voice concierge for the fleet" \
  --initial-prompt-file services/lews/LEWS_CONCIERGE.md
```

The phone finds an open session by role `lews` (or title "LEWS") and reuses it
instead of spawning another. Close it like any other session when it is no
longer needed.
