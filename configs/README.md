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

## Fleet release targets

`fleet_release.example.json` is a separate JSON example for the CLI fleet
installer's explicit `--host-config` argument. It is not a desktop config or
a daemon machines file. Replace its SSH targets and absolute release roots
with your own before running the installer; see
[CLI deployment](../services/agent-orch/deploy/README.md).
