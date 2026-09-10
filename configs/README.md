# Desktop configuration

Configuration files are optional, local, and intentionally boring. The loader
checks the following precedence, from highest to lowest:

1. `PENTACLE_CONFIG`, when it points to an existing file.
2. `configs/<machine-key>.local.js`, ignored by version control.
3. `configs/<machine-key>.js`, a tracked example for a named adapter.
4. `pentacle.config.js`.
5. `pentacle.config.example.js`.

Use a short synthetic machine key such as `local`, `coordinator`, or
`workstation`. Unknown hostnames should be normalized to a safe key; a fresh
installation should fall back to `local`.

## Local override

A local override can select a loopback stream adapter without changing the
tracked example:

```js
module.exports = {
  appName: 'Pentacle',
  hosts: {
    local: { kind: 'local', url: 'ws://127.0.0.1:7797' },
  },
  features: {
    mic: false,
  },
};
```

Set `PENTACLE_CONFIG` to the absolute path of that file before launching the
desktop app. Do not put credentials, personal paths, or service-specific
endpoints in a tracked configuration. Use environment-specific secret stores
outside the repository when an optional adapter requires one.

## Adding a public adapter

1. Add a normalized machine key to the loader's key function.
2. Add focused coverage for precedence and normalization.
3. Keep the adapter loopback-only by default.
4. Document the public fields and their safe defaults.
5. Keep local overrides ignored and reproducible.

