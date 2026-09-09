# Optional local microphone service

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

```bash
cd mic-server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python mic_server.py
```

The default listener is `127.0.0.1:7780`. A client may set
`micServerUrl = "http://127.0.0.1:7780"` in its local configuration. No
non-loopback bind is supported by the public example.

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
