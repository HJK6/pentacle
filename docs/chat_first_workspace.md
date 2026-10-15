# Workspace panes and the chat-first look

Pentacle Web shows sessions in one to four panes. An optional chat-first look
restyles the same workspace. Both use the existing daemon, sessions, chat core,
questions and assets; there is no second chat backend. Electron shares the
renderer but is deprecated; prefer the web host or installed PWA.

## Panes (always on)

- The titlebar **Panes 1 / 2 / 3 / 4** control sets how many panes are visible.
  Four is the classic 2×2 grid with resizable columns. Three shows one tall pane
  on the left and two stacked on the right. Two shows them side by side.
- Reducing the count hides panes without closing their sessions; raising it
  shows them again. Maximize is a temporary focus view, and restoring returns to
  the chosen count.
- Click a pane to make it the target. Selecting a session in the sidebar opens
  it in an empty visible pane, otherwise in the focused pane. Hidden panes are
  never replaced. Selecting a session that is already open focuses its composer
  (Chat) or terminal (Terminal).
- **First visit.** With no saved choice, the workspace starts with one pane.
  If the daemon inventory has exactly one assistant composite, that pane opens
  it exactly as clicking its sidebar row would, including a configured
  direct-assistant binding. Nothing is spawned. With no composite, or more than
  one, the pane stays empty; a composite that appears later still opens, until
  you fill a pane or choose a pane count. Any saved choice always wins.
- The sidebar button collapses the sidebar. On narrow screens the sidebar is a
  drawer: the button opens it, and the backdrop or Escape closes it. The
  numbered navigator shows one pane at a time.
- New sessions open in the configured **Default view** (Settings; Chat unless
  set to Terminal). A plain terminal session with no chat stream opens in
  Terminal. Only Terminal view hands the mouse wheel to tmux, so Chat, Status
  and asset views scroll natively.

## Persistence and privacy

The browser stores the pane count, visible and focused panes, each pane's open
session and view, and unsent drafts in `pentacle.workspace.v1`. On reload, the
saved sessions reopen only if they are still in the daemon inventory; a missing
session is never recreated. Restore runs once, when the first non-empty
inventory arrives. A direct-assistant pane is saved as its assistant entry and
reopens through it, so it binds only a currently valid target. A saved pane
whose session is gone, whose direct target is offline or on another
generation, or whose session is already open in another pane stays empty, and
its saved draft is dropped; a session is never opened twice.
Appearance and feature choices remain in `pentacle.settings.v1`.

These preferences are local to each browser profile, not synced across devices.
Unsent drafts are stored in that profile's local storage, so do not use a
shared browser profile for private work. The daemon credential stays on the web
host and never reaches the browser. Follow
[web host security](../server/README.md#security--auth-and---bind) before
making the service reachable from another device.

## Chat-first look (optional, off by default)

Set `features.chatFirstStyle: true` in your private config, or enable
**Chat-first style** in Settings and reload. It adds warm Light and Dark
palettes (Light when no theme is saved; a saved theme or density wins), system
typography, a taller titlebar with the Pentacle name, restyled sidebar and pane
headers, a "New chat" call to action in empty panes, a composer placeholder and
a restyled transcript. Every rule is scoped to `html.chat-first-style`
(`renderer/chat_first_style.css`), so with the flag off the classic look is
unchanged. Layout and behaviour are the same either way.

## Validation

`npm test` includes `test/chat_first_workspace.test.js` (renderer DOM: bounded
preferences, placement, first-visit default for one, zero and several
composites, a direct-assistant binding and a late composite, saved-choice
precedence, restore from inventory with drafts, direct-assistant persistence
and stale-generation retirement, a session already open before restore, an
empty first inventory, pane controls without
closing sessions, composer focus, wheel routing, sidebar toggle, and the
flag-off/flag-on look). The hermetic web gate (`node test/e2e/web_gate.js`) adds
real-Chrome scenarios `default-pane-assistant`, `pane-count-selector` and
`chat-first-style`, plus the `web_default_pane_gate.cjs` child gate for installs
with and without an assistant composite. Set `PENTACLE_FLAG_OFF_BASELINE_DIR` to
a main run's report directory to compare the flag-off look against main. See
[the E2E harness](E2E_HARNESS.md).

The pane workspace and chat-first look derive from a contribution by Sai
Yeluru (PR #55).
