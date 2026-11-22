# Pentacle Web: next view

The next view is a separate browser entry at `/next/` in the same authenticated web release as `/`. `/next` redirects to the trailing slash. Its assets use root-relative paths under `/next/`; the current entry remains available at `/`.

`renderer/next/entry.js` installs the existing web bridge and uses the shared chat store and transcript renderer. `renderer/next/layout.js` owns the pure card layout transitions, including the shared dashboard or picture slot and lanes rules. The lanes card is independent of chat cards and keeps its navigation state when hidden. `renderer/terminal_view.js` provides the xterm setup shared with the current view. The new composer's capture and delivery paths use the existing browser recorder, voice take and chat bridge.

Lane inventory uses the existing pushed frames and chat-core normalizer. The additive `cc.workLanesShow` bridge supplies complete lane, goal and member details; `renderer/next/lane_reads.js` validates pagination and holds replies only at the current inventory revision. Failed reads remain explicit and can be retried. The lanes view model uses the pinned mobile readability helper, preserves daemon order for lanes without a road, and never infers absent membership from incomplete data.

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
