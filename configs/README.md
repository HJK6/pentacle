# Desktop configuration

Select a private JavaScript config with `PENTACLE_CONFIG=/absolute/path/desktop.config.js`.
When set, this is the only path tried; a missing explicit file is an error.
Otherwise the loader tries `pentacle.config.js` beside the app, then
`pentacle.config.example.js`. Files under `configs/` are not discovered automatically.

Copy the bundled example before editing. Only missing `dark` and `terminal`
objects are backfilled; other settings are not merged with the example.
For a local connection, the supported fields include:

```js
module.exports = {
  appName: 'Pentacle',
  chatStream: {
    url: 'ws://127.0.0.1:7791',
    localHost: 'local',
    hosts: ['local'],
  },
  features: { mic: false },
};
```

Start the daemon separately and match its local identity to `localHost`.
Restart the desktop after changing its config. Keep credentials and private
endpoints outside version control. See [desktop configuration](../docs/desktop_config.md)
for supported fields, routing, presentation, defaults, and warnings.

## Web host profiles

`server/` (web mode) loads a profile the same way the desktop does, selected with
`node server --profile <name>`. The name resolves to `configs/<name>.local.js`,
then `configs/<name>.js`; an argument containing a path separator or ending in
`.js` is used verbatim. Keep private endpoints and topology in a gitignored
`configs/<name>.local.js` (the tracked `<name>.js` stays share-safe).

A web host uses two independent credentials, both server-side (`get-config` and
`/api/config` strip `chatStream.token`/`tokenPath` from the browser):

- **`--token-file <path>`** — the *web login*. Required for any routable
  `--bind`; the browser posts the token once at `/login` for the auth cookie.
- **`chatStream.tokenPath`** (in the profile) — the *chat-stream daemon*
  credential. A credentialed v2 daemon refuses an implicit default path
  (`operator_auth_v2_private_path_required`), so a web host that talks to one must
  name it: a mode-0600 file in a mode-0700 directory, no symlink ancestors.

A profile served to **browser clients** must use the public host shape, not the
desktop-only `remote`/`peers` fields, or the renderer maps peer sessions to the
wrong host:

- `chatStream.hosts` — the desktop host roster (labels, colours, and the set the
  renderer reverse-maps daemon sessions onto).
- top-level `hosts` — `{ id: { host, user, port, tmux } }` SSH transports for the
  non-local roster ids.
- `chatStream.localHost` / `chatStream.hostMap.local` — the daemon identity of the
  machine the web host runs on, so its local sessions attach locally while the
  others attach over SSH.

Field-by-field host semantics are in [desktop configuration](../docs/desktop_config.md)
§ Hosts; the web host's flags, wire protocol and security posture are in
[`server/README.md`](../server/README.md).

## Fleet release targets

`fleet_release.example.json` is a separate JSON example for the CLI fleet
installer's explicit `--host-config` argument. It is not a desktop config or
a daemon machines file. Replace its SSH targets and absolute release roots
with your own before running the installer; see
[CLI deployment](../services/agent-orch/deploy/README.md).
