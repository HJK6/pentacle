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
| `--bind <addr>` | `127.0.0.1` | the interface to listen on — read § Security below before changing it |
| `--token-file <path>` | none | **web login** auth for *this* host; required for any non-loopback `--bind` (§ Security) |
| `--token-path <path>` | none | the **chat-stream daemon** credential (§ Daemon credential) — distinct from `--token-file` |

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

### Daemon credential

A web host that talks to a **credentialed** chat-stream daemon must name the
credential **explicitly**: the v2 operator auth refuses an implicit default path
(it fails the connection with `operator_auth_v2_private_path_required`). Provide
it as `--token-path <path>`, or set `chatStream.tokenPath` in the profile —
either way it stays server-side (stripped from the browser config above). A
loopback daemon with no credential registry needs neither. Example against a
tailnet daemon:

```bash
# --token-path is the daemon credential; --token-file is the web login (the
# routable --bind requires it). Both stay server-side.
node server --profile daffodil \
  --token-path ~/.config/pentacle-stream/token \
  --bind <tailnet-ip> --port 7796 \
  --token-file ~/.config/pentacle-web/daffodil.token
```

## HTTP

| Route | Purpose |
|---|---|
| `/` | `renderer/dist/web/web.html` with the computed config injected as `window.__PENTACLE_CONFIG__` |
| `/api/config` | the same computed config as JSON — byte-identical to what `window.cc.getConfig()` returns |
| `/api/health` | `{ ok, connections }` |
| `/login` | GET the token form, POST the token to mint the auth cookie — present only when auth is on |
| everything else | a file under `renderer/dist/web/`; paths cannot escape it |

`GET /` answers `503` with a build hint when the bundle is missing. When auth is
on, an unauthenticated GET navigation is `302`-redirected to `/login` and an
unauthenticated `/api/*` call is `401`; `/login` is the only open surface.

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

Each connection is capped at 8 concurrent ptys (the renderer uses four slots;
the headroom covers reconnect churn), so one connection cannot exhaust a shared
host by opening unbounded terminals. The (N+1)th `pty:create` on a connection is
refused; re-creating an already-owned slot is a replacement, not a new one.

Host resolution covers `local`, `remote` (`config.remote`), an explicit
`config.hosts` map, **and `config.peers`** — each peer becomes an SSH target
keyed by its id (mirroring the Electron host registry in `hosts.js`), so a
`peers` profile (e.g. daffodil) is attachable in web mode at parity with the
desktop. (Live attach to a *production* peer session is out of scope here — the
smoke rule keeps shared daemons read-only.)

### What never reaches the host

`main/cc_handlers.js` carries three lists, each with a per-channel reason:

- **`WEB_LOCAL`** — the browser answers these itself: `clipboard:read-text`,
  `clipboard:write-text`, `open-external`, `app:reload`. The clipboard is the
  one that matters — routing it here would read the *host's* clipboard, not the
  viewer's — so the host refuses them rather than serving them.
- **`WEB_UNSUPPORTED`** — no *host-side* equivalent, so the host refuses them,
  but the web layer answers each in the browser (§ Native-method browser
  behaviours): `context-menu` → an HTML menu, `meeting:open` → a toast,
  `meeting:close` → a no-op.
- **`UNIMPLEMENTED`** — declared by `preload.js` but served by no main-process
  handler on either transport (the two chat-popout channels and six dashboard
  channels). These are pre-existing desktop gaps, listed so the parity test
  fails if the set grows. The chat-popout pair is deferred to a web-mode popout
  follow-up spec; the dashboards have no provider in the public desktop.

### Native-method browser behaviours

Channels the desktop serves through Electron (a native window, menu, or a host
file write) have no host equivalent for a browser viewer, so `renderer/web_cc.js`
answers them locally — the host still refuses `WEB_UNSUPPORTED` as a safety net:

- **`context-menu`** → an HTML menu (Open in slot 1–4 / Rename / Delete) that
  fires the same `assign-slot` / `action` events `app.js` already listens for.
  (The public desktop registers no `context-menu` handler at all, so this is
  net-new function rather than parity with a native menu.)
- **`meeting:open` / `mic:start-server`** → a "not available in web mode" toast,
  shown only when the mic feature is on.
- **`pty:save-image`** → the pasted image is handed to the viewer as a browser
  download rather than written into the *host's* tmpdir.
- **`open-external`** → `window.open` (a `WEB_LOCAL` channel; unchanged).

## Security — auth and `--bind`

A **loopback** bind (`127.0.0.1`, `localhost`, `::1`) is single-user and needs
no token: any client that can reach the port gets the operator's full
`window.cc`. This is the default and is fine on a machine you control.

A **routable** bind (anything else — `0.0.0.0`, a tailnet IP) **refuses to
start** without `--token-file <path>`; there is no way to publish the surface
unauthenticated. With a token:

- the browser posts the token once at `/login`; the reply sets an `HttpOnly`,
  `SameSite=Strict` cookie carrying `sha256(token)` (never the token itself);
- every page, `/api/*` call and `/cc` websocket upgrade is gated on that cookie
  with a constant-time compare;
- passing `--token-file` on a loopback bind opts that instance into auth too.

The cookie has **no `Secure` attribute**: the host speaks plain HTTP and relies
on the tailnet (or an equivalent private transport) as the encryption boundary.
HTTPS is a named follow-up. Auth gates *who* connects; it does not partition
*what* a connected operator can do — every authenticated connection is equally
the operator, and daemon `chat-stream:frame` traffic is broadcast to all of
them by design. Terminal slots and their pty output are still isolated per
connection (§ Push routing), so two connections never see each other's
terminals.

## Not implemented here

Cross-user authorization (every authenticated connection is the operator),
HTTPS, and profile-switching UX.

## Build

`npm run build:web` → `renderer/dist/web/`. `scripts/build-web.js` derives
`web.html` from `renderer/index.html` rather than copying it, so the desktop and
web pages cannot drift. The UMD and classic scripts (`confirm_dialog.js`, the
dashboard registry and boards, the prebuilt chat-core and cosmic bundles) stay
`<script src>` tags — a CommonJS wrapper would redirect their `window` globals
to `module.exports` — and `app.js` is bundled behind shims for `path` and
`../config-loader`.

## Closed chat slots

When a previously known managed chat disappears from a connected daemon inventory
for 1.5 seconds, its slot returns to the empty state. Its local question portal,
unsent draft and attachment previews are cleared, and its local terminal attachment
is released. This does not send a remote close, interrupt or question response.
Other open slots and their drafts remain intact.

Reconnects and temporary inventory gaps cancel cleanup. Offline or hidden sessions
that remain in inventory stay attached. Session and slot generations prevent an old
timer from clearing a replacement chat; legacy rows without generation identity are
retained conservatively.

## Tests

| File | Covers |
|---|---|
| `test/ws_bridge.test.js` | dispatch, error propagation, malformed frames, pty routing and ownership |
| `test/ws_bridge_multiclient.test.js` | two sockets over the real handler table: slot isolation, broadcast, disconnect cleanup, per-connection cap |
| `test/web_server.test.js` | the real host: HTTP surface, a refused native channel, a live local tmux attach, and two concurrent connections keeping their tmux sessions isolated |
| `test/web_auth.test.js` | the bind guard (routable bind refuses without a token), loopback-needs-none, `/login`, cookie-gated `/api` + `/cc`, and the `--token-path` flag |
| `test/terminal_peer_host.test.js` | terminal host resolution for a `peers[]` profile entry (SSH target), plus local/remote and the unknown-host refusal |
| `test/web_cc.test.js` | the browser shim: method parity with `preload.js`, queueing, reject-on-drop, reconnect, and the native-method shims (toast, save-image download, context menu) |
| `test/web_bundle.test.js` | no Electron/Node requires survive; the page ships everything it references |
| `test/closed_chat_slots.test.js` | retirement deadline, connection authority, generation replacement and slot isolation |
| `test/e2e/web_gate.js` | the deterministic web-mode E2E gate: seeds a loopback daemon and drives the served page in headless Chrome over CDP (`node test/e2e/web_gate.js`; `--profile <config.js>` runs against an external daemon by hand). Scenario functions live in `test/e2e/lib/web_scenarios.js`. Run by the `Public checks` workflow. |

The hermetic web gate also creates two isolated durable questions, self-closes one
seeded chat, and checks slot/portal retirement with survivor preservation. Fixture
self-tokens stay in private scratch files and are removed with the isolated daemon.
The destructive fixture scenario is skipped for external `--profile` runs.
