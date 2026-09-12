# Pentacle web host

Serves the Pentacle renderer to a browser. This process does what the Electron
main process does — owns the tmux attaches and the single chat-stream websocket
connection — and exposes that to a page instead of to a BrowserWindow. Where it
sits in the system is [`docs/ARCHITECTURE.md` § Web mode](../docs/ARCHITECTURE.md#web-mode-headless-host).

```bash
npm run web                       # build the bundle, then serve
node server --profile <name>      # serve an already-built bundle
```

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--profile <name>` | the machine's own config | `configs/<name>.local.js`, else `configs/<name>.js`; a path or `*.js` argument is used verbatim |
| `--port <n>` | `7795` | `0` picks a free port (the startup line prints the real one) |
| `--bind <addr>` | `127.0.0.1` | loopback only by default |

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

## Not implemented here

Auth, multi-connection isolation, tailnet bind and profile-switching UX. The
host is single-user and loopback-only: anyone who can reach the port gets the
operator's full `window.cc`. Do not bind it to a routable address.

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
