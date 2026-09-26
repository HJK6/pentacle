# Web host profiles

The supported desktop client is the browser/PWA served by Pentacle's web host.
The Electron app is deprecated and receives no further upgrades, packaging or
rollout. Configure the host with `node server --profile <name>`. The name resolves
to `configs/<name>.local.js`, then `configs/<name>.js`; an argument containing a
path separator or ending in `.js` is used verbatim. Keep private endpoints and
topology in a gitignored `configs/<name>.local.js`; tracked examples stay
share-safe. See the [web host guide](../server/README.md) for build, run and
authentication details.

## Browser profile shape

A web host uses two independent credentials, both server-side (`get-config` and
`/api/config` strip `chatStream.token`/`tokenPath` from the browser):

- **`--token-file <path>`** — the *web login*. Required for any routable
  `--bind`; the browser posts the token once at `/login` for the auth cookie.
- **`chatStream.tokenPath`** (in the profile) — the *chat-stream daemon*
  credential. A credentialed v2 daemon refuses an implicit default path
  (`operator_auth_v2_private_path_required`), so a web host that talks to one must
  name it: a mode-0600 file in a mode-0700 directory, no symlink ancestors.

The web host resolves local terminals, the `remote` transport, top-level
`hosts` entries, and SSH targets in `peers[]`. For a browser profile, keep the
daemon host roster and the terminal transport mappings aligned:

- `chatStream.hosts` — the host ID roster (labels, colours, and the set the
  renderer reverse-maps daemon sessions onto).
- top-level `hosts` — `{ id: { host, user, port, tmux } }` SSH transports for the
  non-local roster ids.
- `chatStream.localHost` / `chatStream.hostMap.local` — the daemon identity of the
  machine the web host runs on, so its local sessions attach locally while the
  others attach over SSH.

`remote` and `peers[]` remain supported terminal transport inputs. See the
[web host guide](../server/README.md#websocket-protocol-cc) for resolution
behavior and the [shared configuration reference](../docs/desktop_config.md) § Hosts for
the shared client fields.

The web host's flags, wire protocol and security posture are in
[`server/README.md`](../server/README.md).

## Fleet release targets

`fleet_release.example.json` is a separate JSON example for the CLI fleet
installer's explicit `--host-config` argument. It is not a desktop config or
a daemon machines file. Replace its SSH targets and absolute release roots
with your own before running the installer; see
[CLI deployment](../services/agent-orch/deploy/README.md).

## Shared client settings

Browser profiles use the shared client config fields documented in the
[shared configuration reference](../docs/desktop_config.md), including host identity,
terminal transport and optional features. That reference retains Electron
history where needed; the supported setup and deployment path is the web host
described above.
