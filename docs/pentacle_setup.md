# Pentacle web client topology

The supported desktop experience is Pentacle's web client in a browser or
installed as a PWA. The Electron app is deprecated; it receives no further
upgrades, packaging or rollout. The web host serves the renderer and connects to
the separately configured chat daemon, which owns agent processes and session
working directories.

## Local setup

Follow [developer onboarding](developer_onboarding.md) for a scratch daemon and
web host. The root [local setup](../README.md#local-setup) is the complete
developer walkthrough. The web host prints the local URL to open in a browser;
for a shared HTTPS deployment, use the [web host guide](../server/README.md)
and its authentication requirements.

## BRANDING

Use the shipped artwork and palette unchanged, or customize these existing
source files in your own checkout. There is no separate branding config file;
`PENTACLE_CONFIG` and browser profiles configure connections and host identity,
not an arbitrary global brand palette.

### Bring your own logo

Replace the PNGs below, keeping their filenames and dimensions. The browser
favicon, home-screen icon and installed PWA icons are separate assets; replace
all of them for consistent branding.

| File under `renderer/assets/` | Size | Use |
| --- | --- | --- |
| `favicon.png` | 64 × 64 | Browser favicon |
| `favicon-180.png` | 180 × 180 | Apple touch icon |
| `icon-192.png`, `icon-512.png` | 192 × 192, 512 × 512 | PWA icons |
| `icon-maskable-192.png`, `icon-maskable-512.png` | 192 × 192, 512 × 512 | Maskable PWA icons |

Use square PNGs. Ordinary icons may have transparency; maskable icons should
have a full background with the important artwork inside the central safe
area, so a platform's crop preserves it. `renderer/index.html` references the
favicon and touch icon; `renderer/manifest.webmanifest` references the PWA
icons. Keep those paths if replacing files in place. The manifest's `name`,
`short_name`, `background_color` and `theme_color`, and the HTML `theme-color`
meta tag, control installation labels and browser chrome separately from UI
colors. These assets do not replace host sigils or provider badges.

### Bring your own colors

| Source | What to edit |
| --- | --- |
| `renderer/app.js` → `DESIGN_THEME_VARS.dark` / `.light` | Runtime app palette: `accent`, `accentDim`, `blue` (primary buttons), surfaces and text |
| `renderer/styles.css` → dark/light root variables | Matching CSS defaults (`--pc-accent`, `--pc-accent-dim`, etc.) |
| `renderer/cosmic_theme.css` → `.cosmic` | Scoped chat colors such as `--cosmic-green` |
| `renderer/src/cosmic_tokens.ts` → `palette` | Matching component tokens such as `green` |

For example, replace dark `accent: '#7ef0ba'` in `DESIGN_THEME_VARS` and
`--pc-accent: #7ef0ba` in `styles.css` with `#ffb53d`. For primary buttons
such as New Chat, also replace dark `blue: '#3fb950'` in `DESIGN_THEME_VARS`
and `--blue: #3fb950` in `styles.css` with the same color: `blue` is a legacy
token name, and its shipped dark value is green. The runtime palette is
applied as inline CSS variables on startup and when appearance changes; editing
only `styles.css` loses to those runtime values. Edit the light palette too if
you want a custom accent in both appearance modes. To change the chat accent,
change `--cosmic-green` and `palette.green` together; status/severity and host
accents are separate tokens, so keep their semantic colors readable. Host
labels/colors remain configured as described in [desktop configuration](desktop_config.md),
not by replacing global palette tokens. See the [cosmic theme guide](desktop_cosmic_theme.md).

After replacing artwork or editing tokens, run `npm run build:web`, then restart
your web host as usual (`node server --profile <name>` for a named profile).
The host serves a frozen snapshot from startup, so rebuilding without restarting
a running host keeps the old branding. Reload the page and verify the primary
controls and favicon. Choose dark/light
in Settings to inspect both palettes. The build copies assets/CSS and bundles
the token source; editing `renderer/dist/web/` directly is temporary. An already
installed PWA may retain old icons: reinstall it to verify the new installation
artwork, and hard-reload the page if cached artwork persists.

### Start from or reset to the shipped defaults

Before customizing, record `git rev-parse HEAD` as your defaults commit and
copy any existing custom artwork somewhere safe. To reset only branding,
restore the files you changed from that commit, for example:

```sh
git restore --source=<defaults-commit> -- renderer/assets/favicon.png renderer/app.js renderer/styles.css
npm run build:web
```

Include the other icons, `renderer/index.html`, `renderer/manifest.webmanifest`,
`renderer/cosmic_theme.css` and `renderer/src/cosmic_tokens.ts` in that restore
only if you changed them. This discards edits in the named files, so save any
other changes there first. Restart the web host, reload, verify the original logo and colors, and
reinstall an installed PWA if its icon is still cached. Leaving the source
files unchanged uses our defaults from the outset.

## Host profiles and terminals

Choose a browser profile with `node server --profile <name>`. Keep private
endpoints, host inventories and credentials in an untracked
`configs/<name>.local.js`; keep tracked examples generic. The [profile guide](../configs/README.md)
covers profile selection and shape, while [configuration reference](desktop_config.md)
covers shared client settings.

The web host talks to the daemon over its configured WebSocket URL. The daemon
creates sessions and runs providers; the web profile maps daemon host IDs to
terminal transports. See
[web host profiles](../configs/README.md#browser-profile-shape)
and [daemon setup](../services/chat-stream-v2/README.md) before adding remote
machines. A display label alone does not configure a terminal transport.

## Choosing which models the picker offers

The New Chat model picker — on both the web client and the mobile app — offers,
per provider, the models the daemon advertises in its spawn catalog
(`spawn_catalog_get`). The shipped default advertises the full catalog. To narrow
the picker to the models your provider accounts actually expose, set an
`available_models` map in the deployment-local, **never-committed**
`services/_shared/spawn_defaults.local.json` (it sits beside `spawn_defaults.json`
and is git-ignored):

```json
{
  "schema_version": 1,
  "available_models": {
    "claude": ["claude-opus-4-8", "claude-opus-5-5", "claude-fable-5-1"],
    "codex":  ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"]
  }
}
```

- **Per provider, canonical ids.** Keys are providers (`claude`, `codex`); each
  value is a non-empty list of canonical model ids drawn from the catalog (see
  `agent-orch models`). An unknown provider or id fails the config closed rather
  than silently dropping it.
- **List order is display order.** The picker shows each provider's models in the
  order you list them, on web and mobile alike. Omit a model to hide it.
- **Display-only.** Narrowing affects only the New Chat picker. The full catalog
  stays authoritative for everything else: existing seats, explicit launches by
  full id, aliases, handoffs and the configured spawn defaults all keep working
  against models you have hidden — so hiding a model never breaks a running seat
  or a spawn default.
- **Omit the key for the default.** With no `available_models` (the shipped
  state), the full catalog is offered.
- **Applies after a daemon restart.** The daemon caches the policy for its
  process lifetime, so repin/restart the daemon to pick up an edit.

Each user or installation owns its own `spawn_defaults.local.json`, so different
deployments present different picker lists from the same public code.

## Web client controls

Machine sigils identify the host for each session. Each terminal slot has a
Copy Chat ID control in its header. Provider usage appears in the sidebar when
`features.usage` is enabled and the daemon limits collector supplies data. Once
the host serves a build with update checking, each open browser window
independently shows a refresh control beside Settings when that window's loaded
build is older; refreshing updates that window.

The top and bottom terminal rows have independent column dividers, and each row
keeps its preferred split on this browser origin; the [shared configuration
reference](desktop_config.md) covers divider controls and persistence. In the
experimental structured Chat view, durable question cards support typed
free-text answers, including prompts without choices.

The web client also supports microphone input and voice actions through a
configured host-managed service. See [local voice action delivery](local_voice_actions.md)
for the current behavior and setup.

## Verification

| Symptom | First check |
|---|---|
| Sidebar is empty | Check the WebSocket URL and `agent-orch list`. |
| Experimental structured Chat is unavailable | Enable `features.chatUi` and inspect the daemon health result. |
| Terminal attach fails | Check the selected host ID and its configured tmux transport. |
| Local daemon exits | Run the daemon command directly and inspect its startup error. |

### Single-machine appearance

Machine badges hide automatically when the fleet has exactly one host. The
sidebar, machine filter, spawn picker and machine stats show no machine icon
or initial-letter fallback in that case. With two or more configured or
discovered hosts, all machine badges return, including for offline hosts. No
setting is required. Host labels, accents and assistant identity artwork remain.
