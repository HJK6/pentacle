# Pentacle web host

Serves the Pentacle renderer to a browser. This process does what the Electron
main process does — owns the tmux attaches and the single chat-stream websocket
connection — and exposes that to a page instead of to a BrowserWindow. Where it
sits in the system is [`docs/ARCHITECTURE.md` § Web mode](../docs/ARCHITECTURE.md#web-mode-headless-host).

## Prerequisites

Node 20+ and `npm install` at the repo root. Beyond that: `tmux` on whichever
host the terminals live on, Python 3 if you want to run the chat daemon locally,
and Chrome or Chromium on `PATH` only if you intend to run the smoke.

## Run it

```bash
npm install
npm run web                       # build the bundle, then serve
node server --profile <name>      # serve an already-built bundle
```

The process prints the URL and stays in the foreground; it does **not** open a
browser. Open the printed `http://127.0.0.1:7795` yourself. Without a daemon to
talk to, the page loads and the terminals work, but the sidebar is empty — start
one first if you want sessions:

```bash
SCRATCH=$(mktemp -d)
python3 services/chat-stream-v2/main.py --host 127.0.0.1 --port 7796 \
  --local-host local --db "$SCRATCH/sessions.db" \
  --notifications-db "$SCRATCH/notifications.db" \
  --assets-db "$SCRATCH/assets.db" --blob-root "$SCRATCH/blobs" \
  --disable-hosts --disable-mirror --disable-nudges \
  --disable-outbound-notices --disable-remote-presence
```

then point the host at it — `test/e2e/configs/web_mode_local_smoke.js` is
exactly that config, so `node server --profile test/e2e/configs/web_mode_local_smoke.js`
works as-is.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--profile <name>` | see below | `configs/<name>.local.js`, else `configs/<name>.js`; an argument containing a path separator or ending in `.js` is used verbatim |
| `--port <n>` | `7795` | `0` picks a free port (the startup line prints the real one) |
| `--bind <addr>` | `127.0.0.1` | the interface to listen on — read the warning below before changing it |

With no `--profile`, the host does **not** look for `configs/<machine>.js`: it
follows `config-loader`'s ordinary precedence — `PENTACLE_CONFIG` if it is set in
the environment, then `pentacle.config.js`, then `pentacle.config.example.js`. A
`--profile` that resolves to nothing is not fatal: the host logs
`using fallback configuration` and starts with a minimal built-in config, which
is why an empty sidebar can mean a mistyped profile rather than a dead daemon.

The profile is loaded through `config-loader` exactly as `main.js` loads it, so
the browser sees the config the desktop would see. `get-config` and
`/api/config` both answer with the same computed object, and both strip
`chatStream.token` and `chatStream.tokenPath` — the daemon credential never
reaches a browser.

## HTTP

| Route | Purpose |
|---|---|
| `/` | `renderer/dist/web/web.html` with the computed config injected as `window.__PENTACLE_CONFIG__` |
| `/api/config` | the same computed config as JSON — byte-identical to what `window.cc.getConfig()` returns |
| `/api/health` | `{ ok, connections }` |
| everything else | a file under `renderer/dist/web/`; paths cannot escape it |

`GET /` answers `503` with a build hint when the bundle is missing.

## Websocket protocol (`/cc`)

JSON, one message per frame. One socket per browser tab.

```jsonc
// browser → host
{ "id": 7, "method": "pty:create", "args": [0, "sess", "local", 80, 24] }
// host → browser
{ "id": 7, "ok": true,  "result": "%12" }
{ "id": 7, "ok": false, "error": { "code": "handler_error", "message": "…" } }
// host → browser, unsolicited
{ "event": "pty:data", "args": [0, "…"] }
```

`method` is an IPC channel name from the shared definitions in
[`main/cc_handlers.js`](../main/cc_handlers.js), which `main.js` registers on
`ipcMain` and this host collects into a table. The `mode` mirrors `preload.js`:

- **invoke** channels answer exactly once, with `ok: true` and a `result` (a
  handler returning nothing answers `null`), or `ok: false` and an `error`.
- **send** channels (`pty:write`, `pty:resize`, `pty:scroll`, `pty:tmux-send`,
  `pty:exit-copy-mode`, `perf-telemetry:record`, `context-menu`) are
  fire-and-forget and are **never** answered — including when the handler
  throws — exactly as `ipcRenderer.send` behaves.

Error codes: `bad_request` (unparseable frame or no `method`),
`unknown_method`, `web_local`, `web_unsupported`, `handler_error`. A refusal
obeys the same contract as a dispatch: a refused **send** channel is answered
with silence, never an unsolicited error frame.

### Push routing

Each connection gets a stand-in for Electron's `event.sender`, so the handlers
route themselves. `main/terminal_adapter.js` keys its slots by
`event.sender.id` and pushes `pty:data` / `pty:exit` through
`event.sender.send`, which means one tab's terminals and output can never reach
another's — no slot bookkeeping lives in the bridge. Closing a socket fires the
adapter's `destroyed` hook, the same signal a closing window gives, so a reload
cannot leak attachments; the tmux session itself is untouched. Daemon frames
(`chat-stream:frame`) are broadcast to every connection.

### What never reaches the host

`main/cc_handlers.js` carries three lists, each with a per-channel reason:

- **`WEB_LOCAL`** — the browser answers these itself: `clipboard:read-text`,
  `clipboard:write-text`, `open-external`, `app:reload`. The clipboard is the
  one that matters — routing it here would read the *host's* clipboard, not the
  viewer's — so the host refuses them rather than serving them.
- **`WEB_UNSUPPORTED`** — no browser equivalent: `context-menu`,
  `meeting:open`, `meeting:close`.
- **`UNIMPLEMENTED`** — declared by `preload.js` but served by no main-process
  handler on either transport (the two chat-popout channels and six dashboard
  channels). These are pre-existing desktop gaps, listed so the parity test
  fails if the set grows.

## Security posture — read before changing `--bind`

There is **no authentication of any kind**. Any client that can open a socket to
the port gets the operator's full `window.cc`: every terminal, every daemon RPC,
every file the handlers can reach.

The *default* bind is `127.0.0.1`, not a property of the host — `--bind` accepts
any address, and `--bind 0.0.0.0` publishes that unauthenticated surface to
every machine that can route to this one. Do not use a non-loopback bind until
token authentication and deployment-level access control exist.

## Not implemented here

Authentication, cross-user authorization, a tailnet bind, and profile-switching
UX. Note what this does *not* mean: terminal slots and their pty output are
already isolated per websocket connection (see § Push routing), so two tabs
cannot see each other's terminals. What is missing is any notion of *who* is
connected — every connection is equally the operator. Daemon `chat-stream:frame`
traffic is broadcast to all connections by design.

## Build

`npm run build:web` → `renderer/dist/web/`. `scripts/build-web.js` derives
`web.html` from `renderer/index.html` rather than copying it, so the desktop and
web pages cannot drift. The UMD and classic scripts (`confirm_dialog.js`, the
dashboard registry and boards, the prebuilt chat-core and cosmic bundles) stay
`<script src>` tags — a CommonJS wrapper would redirect their `window` globals
to `module.exports` — and `app.js` is bundled behind shims for `path` and
`../config-loader`.

## Tests

| File | Covers |
|---|---|
| `test/ws_bridge.test.js` | dispatch, error propagation, malformed frames, pty routing and ownership |
| `test/web_server.test.js` | the real host: HTTP surface, a refused native channel, a live local tmux attach over the wire |
| `test/web_cc.test.js` | the browser shim: method parity with `preload.js`, queueing, reject-on-drop, reconnect |
| `test/web_bundle.test.js` | no Electron/Node requires survive; the page ships everything it references |
| `test/e2e/web_smoke.js` | headless Chrome against a real daemon — run by hand: `node test/e2e/web_smoke.js --profile test/e2e/configs/web_mode_local_smoke.js` |
