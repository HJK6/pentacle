# Pentacle Web: next view

The next view is a separate browser entry at `/next/` in the same authenticated web release as `/`. `/next` redirects to the trailing slash. Its assets use relative paths; the current entry remains available at `/`.

`renderer/next/entry.js` installs the existing web bridge and uses the shared chat store and transcript renderer. `renderer/next/layout.js` owns the pure card layout transitions, including the reserved dashboard or picture slot and lanes rules. The first slice mounts only Assistant and chat cards. `renderer/terminal_view.js` provides the xterm setup shared with the current view. The new composer's capture and delivery paths use the existing browser recorder, voice take and chat bridge.

Theme values are declared at the start of `renderer/next/style.css`. Dark is the default; the other themes are stored under `pentacle.next.theme`, separate from the current Web settings. The CSS after the token block uses those tokens.

Run `npm run build:web` to build both entries. The `next-*` scenarios in `test/e2e/lib/next_scenarios.js` run in the hermetic web gate; for a focused run use `node test/e2e/web_gate.js --scenario next-home-rail` with the prepared gate Python environment. The full release gate uses the `web-gate` target in `scripts/gate_env.py`.
