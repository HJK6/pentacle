# Chat-first workspace

This checkout adds a chat-first presentation to Pentacle Web. It uses the existing
daemon, provider sessions, shared chat core, receipts, questions and assets; it
does not replace them with a second chat backend. Electron uses the same renderer
but remains deprecated upstream. Prefer the web host or installed PWA.

## Daily use

- Choose a conversation in the left sidebar, or **New chat** to select a host and
  provider. Ordinary agent sessions open in **Chat**. Search filters the sidebar.
- Scroll and select conversation text normally. **Load earlier** retrieves older
  history. While reading above the bottom, incoming messages do not pull you
  away; the down-arrow returns to the latest message.
- Write in the bottom composer. **Enter** sends, **Shift+Enter** adds a line.
  The plus button adds images. Slash text is sent verbatim; CLI-only commands,
  interactive provider menus and shell commands belong in **Terminal**.
- The **1 / 2 / 3 / 4** toolbar controls choose the visible pane count. Click a
  pane to target it. Sidebar selection uses an empty visible pane, otherwise
  replaces the focused pane. Right-click a session to choose a particular slot.
  Reducing the count hides panes without closing their sessions. Focusing a
  visible pane leaves the other visible panes in place. Maximize is a
  temporary focus view; its restore button returns to the chosen layout.
- **Status**, report tabs, questions and the terminal fallback remain available
  per pane. Terminal attachment is lazy: failure to attach a PTY does not remove
  the conversation, and Terminal offers a retry.
- The sidebar button toggles navigation. On narrow screens it opens a drawer;
  the numbered pane navigator selects one readable pane at a time.
- Settings includes **Light / Dark**, density, and the **Chat workspace** feature.
  An explicit existing opt-out is respected. Enable the feature and reload to
  migrate a terminal-first installation.

## Persistence and privacy

The browser stores pane count, visible slots, focused slot, open session identifiers, selected
views and unsent drafts in `pentacle.workspace.v1`. Restore only attaches
sessions still present in the daemon inventory; it never spawns a replacement for
a missing session. Text selection, history and delivery state use the existing
shared model. Appearance remains in `pentacle.settings.v1`.

These preferences are local to each browser profile, not cross-device storage.
Do not use a shared browser profile for private work. The daemon credential stays
on the web host and is omitted from browser configuration. Follow
[web host security](../server/README.md#security--auth-and---bind) before making
the service reachable from another device. Native mobile connects to the same
daemon and retains its own enrollment and UI.

## Setup and validation

Use the [setup guide](../AGENT_SETUP.md), build with `npm run build:web`, and start
`node server` with your private config. Keep the daemon running. Default web
binding is loopback; no new account or credential is required for a previously
authenticated daemon connection. Voice and provider quota displays still require
their documented backends; UI installation alone does not enable them.

`npm test` includes the full-renderer DOM regressions in
`test/chat_first_workspace.test.js`: initial chat, no hidden PTY attachment,
composer focus, native status scrolling, literal slash input/offline sends,
bounded layout state, non-destructive pane counts, inventory-only restoration,
drafts and failed-terminal recovery. Existing shared transcript, question,
attachment and reconnect tests remain required. A visual browser check and real
provider reply establish runtime activation; unit tests alone do not.
