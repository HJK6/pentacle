# Optional local microphone service (contract)

This directory documents a privacy-minimized, loopback-only microphone adapter.
It is optional and is disabled unless explicitly enabled by the local
configuration.

## Principles

- Bind to `127.0.0.1` by default.
- Keep captured text and audio state in memory only.
- Do not upload audio, copy it to a remote host, or write transcripts by default.
- Expose a small status and mode API so a desktop client can show consent state.
- Treat every mode change as an explicit user action.

## Run

This repository ships the contract and the helper modules (`audio_device.py`,
`clipboard.py`), not a server entrypoint. Run a service that implements the
API below, bound to `127.0.0.1:7780` (the client default), and point the
desktop or web host at it with `micServerUrl` (see
[desktop configuration](../docs/desktop_config.md)). Keep `features.mic`
disabled until that service answers `GET /status`. No non-loopback bind is
supported by the public contract.

## Minimal API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/status` | Current mode and capture health |
| `POST` | `/mode/on` | Begin an explicitly requested local mode |
| `POST` | `/mode/off` | Stop capture and clear in-memory state |
| `POST` | `/calibrate/start` | Begin a local calibration sample |
| `POST` | `/calibrate/stop` | End calibration and discard the sample |

POST bodies are JSON. A successful response has the shape
`{"ok": true, "mode": "on"}`; failures use
`{"ok": false, "error": "..." }`. The service rejects requests from
non-loopback clients.

## Data handling

The public adapter should report only coarse health fields such as
`stream_open`, `selected_device`, and `health_state`. It should not expose
audio payloads or personal text through its status response. If a downstream
application wants to save data, it must ask for consent and document its own
retention policy separately.
