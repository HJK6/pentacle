# Pentacle Web: next view

The next view is a separate browser entry at `/next/` in the same authenticated web release as `/`. `/next` redirects to the trailing slash. Its assets use root-relative paths under `/next/`; the current entry remains available at `/`.

`renderer/next/entry.js` installs the existing web bridge and uses the shared chat store and transcript renderer. `renderer/next/layout.js` owns the pure card layout transitions, including the shared dashboard or picture slot and lanes rules. The lanes card is independent of chat cards and keeps its navigation state when hidden. `renderer/terminal_view.js` provides the xterm setup shared with the current view. The new composer's capture and delivery paths use the existing browser recorder, voice take and chat bridge.

Lane inventory uses the existing pushed frames and chat-core normalizer. The additive `cc.workLanesShow` bridge supplies complete lane, goal and member details; `renderer/next/lane_reads.js` validates pagination and holds replies only at the current inventory revision. Inventory pushes mostly change presence and working counts, which `detail_revision` excludes, so a held or failed read is read again only when the revision changes or once its result is ten minutes old (worked time is outside the revision too). Failed reads remain explicit and Retry reads at once. The lanes view model uses the pinned mobile readability helper, preserves daemon order for lanes without a road, and never infers absent membership from incomplete data.

The questions view consumes the existing notification list and frames, answer operation and prompt reconciliation. Its renderer places a spec question only with positive lane membership, keeps uncertain answers from being sent twice, and reads stored answers for the spec's “You said” line. The shared durable-question model supplies the current and next views; transcript name links are limited to unambiguous known lane and goal titles.

Design and later dashboards use the layout module's `dash` card. The dashboard supplies its own header, including full screen and close. On a phone the layout presents one main pane and hides the full-screen control. Opening Design fetches the current catalog, then checks it every ten seconds with the last entity tag. Home refreshes in place. An open board offers the newly published version with Show and keeps the camera and the current screen when available. Reload is in the title menu and phone pull-down refreshes Home.

The workspace addresses are `/next/design`, `/next/design/<project>/<design>` and `/next/design/<project>/<design>/<screen>`. The app's Design-only page uses the same forms under `/next/embed/design`. This page requires the web host's Tailscale identity mode, uses cosmic and phone controls at every width, and loads the catalog and media from that host. Both entries use the shared viewer and replace the current address as the reader navigates. The shared path builder comes from `HJK6/pentacle-chat-core` release `b486edfbcb7c03a8e847dd5097d9a5e7100af69b`. Prepared screens keep their opaque frame sandbox.

On desktop, choose **Dashboards → Melody** to play a one-note-at-a-time piano. The piano and timelines show note names. Click a history note to append it to your composition, or drag across notes and append the selection to preserve its timing. After a pause of three default gaps, the next note starts one default gap after the previous note. Select a composition note to change its preceding gap, pitch or position, or to delete it.

Save and Save As keep named compositions in this browser profile on this web origin. Close starts an empty board. An authenticated deployment can also offer a starter composition to every browser by setting `PENTACLE_MELODY_FEATURED_FILE` to a private JSON file. Its fields are `id`, `name`, `defaultGapMs` and `notes`. Each note has an integer MIDI pitch from 24 to 83, a positive `durationMs` and a nonnegative `gapMs` before it. The first gap is zero. Saving edits to the starter takes precedence for that browser.

Theme values are declared at the start of `renderer/next/style.css`. Dark is the default; the other themes are stored under `pentacle.next.theme`, separate from the current Web settings. The CSS after the token block uses those tokens.

Run `npm run build:web` to build both entries. The `next-*` scenarios in `test/e2e/lib/next_scenarios.js` run in the hermetic web gate; for a focused run use `node test/e2e/web_gate.js --scenario next-home-rail` with the prepared gate Python environment. The full release gate uses the `web-gate` target in `scripts/gate_env.py`.

After an authorized web activation, `scripts/web-next-live-proof.cjs` checks the installed release over HTTPS at desktop and phone widths without creating a chat. Set `REPAIR_APP` to the immutable app directory, `REPAIR_SHA` and `REPAIR_PID` to the serving release and process, `REPAIR_NEXT_SHA256` to its next bundle hash, `REPAIR_ORIGIN` to the HTTPS origin, and `REPAIR_PROOF_DIR` to an owned evidence directory. The script verifies the PID and listener before and after its browser checks, records screenshots and comparison values, waits up to ten seconds for machine rows, and waits for the current web's catalog to settle before comparing choices. `CHROME_EXECUTABLE_PATH` can select a Chrome executable; otherwise Playwright uses its Chrome channel.

### Arrange the desktop workspace

At 600 px and wider, the icon rail starts on the left. **Settings → Sidebar**
moves it to the top or back to the left. Like Theme, that choice is saved in
this browser profile. Pane placement, widths and open cards reset on reload.

The stage has room for up to two main panes plus Lanes. Choose a chat from
**Chats** to show it beside the last-used pane, replacing the other pane as
needed. Choosing a chat already visible just uses it in place. Both chats have
their own draft and transcript position. Mode belongs to the card and resets
to thread when it changes chats. Closing a card does not close its session. Starting a recording in another pane stops and discards the
previous recording. A dashboard link opens beside the chat that contains it.

The Assistant's rail mark sends the Assistant aside without closing a chat.
Press it again to return the pane if there is room, or open it over the stage
when there is not. Escape, another press of the mark, or a press outside the
overlay dismisses it. **Keep [Assistant name] on screen** returns it to the
stage, replacing the chat not used last when both chats were showing, or
dismissing the open card when a narrow desktop cannot fit it beside the
Assistant. Its session stays in Chats. The Assistant comes back automatically if the last open section closes.

Drag a section's header with the primary pointer to rearrange it. The side
strips dock a section to a half; the top strip makes a chat or dashboard
full-screen. Move deliberately into a strip, or farther into it if the drag
began there. Lanes can move without resizing. Press Escape while carrying a
section to cancel. Resize adjacent panes by dragging the gap between them;
double-click a resize gap to reset all sizes. There is no resize gap next to
Lanes. The phone layout still shows one section at a time and does not change.

### Chat tools in Next

Use a chat's title to switch it in place. Picking the chat already in the
other card swaps the two sessions without moving their seats. The rail still
opens chats with its existing placement rules. Both lists show working and
unread state and reveal Rename and Delete on hover or keyboard focus. An open
chat's header also shows Rename and Delete, beside Status, Terminal, full screen
and close. Rename edits the title in place. It saves on Enter or blur, and
Escape cancels. Delete asks in the row or the header before closing the actual
session, and the configured assistant cannot be renamed or deleted. Full screen
and Leave full screen have different icons: arrows pointing out, and arrows
pointing in.

A chat closed anywhere else (another client, the agent itself, or the daemon)
leaves the screen too. Its card closes when the daemon's open inventory marks the
chat closed, or when the chat drops out between two inventories on the same
connection. Every snapshot, with or without sessions, and every reconnect start a
fresh view: only a later inventory sets the baseline, because a new view can see a
different set of chats. A chat
that was just started, and is not listed yet, stays open.

An open Delete question closes if the card is covered (including by the
Assistant overlay), hidden or changes full screen, and Escape answers it before
anything else. Rename and Delete show only
for a listed chat that the list could also rename or delete.

An unread chat has a latest non-operator message different from the one last
displayed while the page and the card were visible. The Assistant has the same
check on its mark when aside. The first inventory seeds the browser's marks.
Identical consecutive kind/text pairs count as one; an offline host does not
erase marks. The marks stay in this browser under `pentacle.next.chat.seen`;
if local storage is unavailable they last only for the page.

The working row sits above the composer, uses the live elapsed clock or a
chat's reported label, and leaves off the time if neither is known. The
Assistant uses its own elapsed clock only. The first reported time sets the
clock, which then counts in whole seconds in the browser. A later report
moves it only when the two disagree by more than 2.5 seconds; exactly 2.5
seconds counts as heartbeat delay. Only times on screen count: a pane that is
covered or off screen, or a card showing its terminal, stops counting and
shows no stale time, and its time is current again when it returns or leaves
the terminal. Reduced motion slows the working
ring, makes the Assistant's mark still, and stops the send chevrons. The queue
line only offers Send now when the store permits it for that specific message.
The shared transcript and ordinary desktop/web send behavior are unchanged.

### Chat history and message formatting

The Assistant and the chats in the cards actually on screen are the store's
focused streams, so traffic in other chats cannot evict their messages. A pane
that is closed, covered by a full-screen card or squeezed off the stage is
released. A layout change sets focus once, after every card has its chat and
visibility, so a covered chat is not read while hidden. `renderer/next/chat_history.js` applies the current web's history
rules through `renderer/chat_events_lazy.js`. A chat is read once the daemon is
connected and again after it returns to the screen, after the page reconnects,
and after the daemon reconnects (its `__reconnect` frame), including when the
daemon's restart grace hid the disconnect. A complete read with no events shows
**No messages yet.** without a retry. A failed or incomplete read retries after
1, 2, 4, 8 and 16 seconds. Until messages arrive the transcript says **Loading
messages…** or **Reconnecting…**; when the retries are spent it offers
**Retry**.

A transcript opens on its latest 120 rows. **Show earlier messages** reveals 48
more, asking the daemon for older pages when the store holds none, and scrolling
up near the top does the same. Where older history starts is taken from the
newest read; an empty first reply is replaced by a later retry that finds more,
and once older pages are read a fresh read keeps the deeper place. Paging stops
as soon as the pane shows another chat or the connection drops, and a request
from before a chat switch never reveals rows in the pane after it. The reader's place is kept when earlier rows are
added, when new messages arrive further down, and while text is selected. A
reader at the latest message stays there when the pane changes width or late
content loads, but a row they open there is left where they clicked it. A card
that was hidden or showed another chat returns to its reader's place once the
stage has its final widths.

Messages from other agents, daemon notices and child reports are compact rows
that open to the full text and stay open across updates. A run of consecutive
agent reports, or of answered questions, is one group. An opened group stays
open while it shares a member with the group that was open, so it survives
items joining at either end, and a row opened on its own that joins a group
opens the group with it. A group its reader closed stays closed. Tables, code, commands,
file changes and tool results have their own styles in the conversation section
at the end of `renderer/next/style.css`. Text, code and agent rows wrap to the
pane, so a chat never scrolls sideways. The transcript renderer marks a table
cell showing 18 characters or fewer `is-short`; headers and short cells keep to
one line while the table's frame is wider than 520 px, and short cells wrap
like the rest in a narrower frame. A table with more columns than fit scrolls
inside its own frame. On a phone, agent, daemon and report rows hide their time.
**Loading messages…** or **Retry** alone in a chat sits mid-pane.

Terminal output reaches both cards through one listener in
`renderer/next/entry.js`, because the web bridge keeps a single listener per
event.

### Loading the latest version

A page knows the build it was served (`buildId` in the injected config) and asks
the host which build it serves now (`cc.getBuild`) on reconnect, when the page
becomes visible and every ten minutes. While the host serves a different build,
the rail shows **Load the latest version** above Limits in the accent colour
(beside Limits on a phone); a click reloads. Nothing reloads by itself, because
a reload resets open cards and the pane layout. `renderer/next/update_notice.js`
holds the comparison: an unknown build never shows the control, a failed check
keeps the last answer, and a host rolled back to the page's build withdraws it.

### Reading width, microphone and scrollbars

**Settings → Text width** sets one reading-column cap for Assistant and
chat transcripts, their composers and voice displays, status views and
working rows. Type a whole number from 320 to 2400 px, or use the
480–1200 px slider. The exact field value remains authoritative between
slider steps. Guides show while either control has focus. Narrow panes
keep their margins; terminal, Lanes and dashboards do not use this cap.
The default is 720 px; preferences stay in this browser.

**Microphone** appears when the browser can name more than one physical
input. **System default** is first. A selected microphone applies across
all Next conversations. If only that input remains, the picker hides but
the selection stays active. If the input disappears or the browser says
it cannot be found, Next forgets it and the following recording uses the
default. Permission and busy errors do not reset a choice. The current Web
MIC panel and its preference are separate.

Native thumbs are quiet at rest and clearer on hover or for 900 ms after
scrolling. They widen under the pointer and take the theme accent when
dragged. Conversations reserve equal gutters to avoid shifting; lists and
message fields have a narrower bar. Design tabs keep their hidden scrollbar
and terminals keep their existing scroller.

## Pictures beside their chat

In the new view, click an ordinary image thumbnail to open Picture in the shared
slot used by Design. The outlined thumbnail closes the card when clicked again;
another thumbnail replaces it. Desktop Picture uses the fluid stage and defaults to even widths with
its source chat. Close returns to the prior chat placement, including a
full-screen chat. The dim assistant logo brings the assistant chat beside Picture;
choosing a chat from the rail ends the picture arrangement. When room is
tight the picture is in front and the chat waits behind it. The full-screen
control is available only at 600 CSS pixels and above; Escape leaves full
screen. Lanes retain their own state.

Ordinary images use the existing attachment read and stay separate from typed
Design previews, which take over the same slot. Ordinary Markdown links,
including `/designs/`, continue opening in a new tab. The current web's
attachment viewer, file downloads, and the terminal remain unchanged.

A message displays Sending while delivery is being resolved. A landed receipt displays Sent; a committed send still awaiting proof displays Sent after 20 seconds, and remains checked for later receipt changes. A not-landed receipt displays Failed sending with Retry and an X to discard the local message. Failed retries use a new request ID. Receipt checks run after the reply, at 20 seconds, then after 15, 30, 60, 120 and up to 240 seconds between checks, and on reconnect. A successful lookup with no receipt after reconnect marks the send Failed once no dispatch is in flight; lookup errors leave it unresolved. Tracking ends on proof, failure, discard, or after 30 minutes.
