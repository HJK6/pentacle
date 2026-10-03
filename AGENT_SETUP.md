# Agent setup: Pentacle

Give a capable coding agent, such as Fable or Astra, this file's path and ask it
to complete the setup. The agent should read the linked instructions, inspect
the computer, carry out the steps and verify the result. You do not need a
separate chat-core checkout.

## Before starting

Set up Pentacle Web (the recommended client), mobile, or both. The daemon runs
on the computer where your coding agents work; Pentacle Web (a browser/PWA
served by the Node web host on that computer) and the phone are clients of that
daemon. The Electron desktop app is deprecated; do not set it up for new users. Start with one host named `local` and one
working provider. Add other hosts later.

The daemon and web host run on macOS and Linux. An iPhone build needs a Mac
with Xcode. Use an existing authenticated Claude or Codex CLI, or let the agent
install/configure one and request the account login when needed. Phone signing,
trust and unlock actions may require the owner. These are setup prerequisites,
not tasks the agent should pretend it completed.

## Ask the owner during setup

Early in setup, ask the owner these simple questions and apply the answers:

- Do you want the **always-on assistant**? If so, what **name** and **icon**
  should it use? (Default name in this kit is **Bart**.)
- Do you want to connect a **ChatGPT "Dot"** agent for large assignments?
- Do you want to change the app **icons or colours** from the shipped defaults?
- What do you want to **call your machines** (display names)?

Keep the defaults if the owner has no preference.

## Agent checklist

1. **Inspect first.** Read [README.md](README.md) and the relevant repository's
   `AGENTS.md`. Check the OS, installed tools, provider login, existing config and
   whether a daemon already listens on the intended port. Preserve existing
   configuration and user files. Do not start a duplicate daemon or replace a
   working credential unnecessarily.
2. **Prepare the daemon host.** Follow [Local setup](README.md#local-setup):
   Node.js 22.12+, Python 3.11+, tmux, dependencies and provider paths. Use a
   private workspace outside this checkout. For mobile-only use, the daemon
   needs the Python dependencies, tmux and provider CLI; installing or running
   the Electron desktop is not needed.
3. **Configure one consistent host.** Use `--local-host local`, `local` in client
   host lists, the real provider executable paths, and the real transcript
   directory. Store the database/config/credentials outside the public repo.
   Start the documented daemon command in a persistent terminal or service and
   record how to stop and restart it.
4. **Set up Pentacle Web.** Issue its daemon credential, build it with
   `npm run build:web` and serve it with `node server` as in the README (behind `tailscale serve` with identity auth for
   remote use — see [server/README.md](server/README.md)); open the URL in a
   browser or install it as a PWA. The Electron desktop app is deprecated (no
   upgrades); set it up only if the owner explicitly asks for it. Choose Chat under Default view or set `features.defaultChatView: true`; preserve an explicit
   Terminal-first preference. Verify sidebar selection opens Chat, native scrolling works, and
   the bottom composer receives input. Terminal remains available per pane.
   Read [config reference](docs/desktop_config.md). For multiple hosts,
   populate hostNames/hostColors and exact chatStream.hostMap aliases; verify
   the local badge. The public [microphone service](mic-server/README.md) includes the runnable
   `mic-server/mic_server.py` entrypoint and starts with capture off. Keep mic disabled
   until its devices, models and endpoint are provisioned and verified,
   and enable features.usage only with the daemon limits collector configured.
   Machine-stat cards come from daemon hosts.stats frames (hosts payload); machineStats is ignored.
   Preserve the private overlay and existing saved Settings during upgrades.
5. **Set up mobile if requested.** Clone
   [pentacle-mobile](https://github.com/HJK6/pentacle-mobile), then follow its
   [mobile setup guide](https://github.com/HJK6/pentacle-mobile/blob/main/AGENT_SETUP.md).
   Configure an endpoint the device can reach and issue its single-use link on
   the daemon host using `services/chat-stream-v2/tools/mobile_enrollment_cli.py`.
   Read [network access](docs/REMOTE_AUTH.md) before changing the listener.
6. **Verify real use.** Confirm the daemon stays running. In each requested
   client, create a session on `local`, send a short message and observe the
   provider reply in the actual terminal or mobile transcript. Passing unit
   tests or launching a window alone does not establish a connected setup.
   Do not repeat the repository's full test suites just to install the app.
7. **Finish with a short handover.** Report the installed source revision,
   config paths, daemon endpoint, start/stop commands and the observed reply.
   Keep tokens and enrollment links out of the report. If an owner login,
   signing or device action remains, name that exact step and resume after it;
   do not claim the setup is complete.

## Configure metrics, memory sync and operator questions

Follow [the agent metrics and memory guide](docs/AGENT_METRICS_AND_MEMORY.md) to
set up Claude account limits, local/remote machine stats, Syncthing shared
memory, and questions through Updates. By default operator questions route
through `agent-orch prompt ask` (asynchronous and durable; the operator answers
in Updates); a fleet that prefers different behavior injects its own override on
top of that default. The daemon already disables Claude's native
`AskUserQuestion` tool. Include these requested features in the handover and
verify them using the guide's acceptance checks.

## If a step fails

Use the error to narrow the next action: a refused socket needs a running,
reachable daemon; an authentication error needs the matching client credential;
an unavailable provider needs a working provider CLI and login. A physical
phone's `localhost` is the phone itself. Check these before rebuilding clients.

The shared library is public at
[pentacle-chat-core](https://github.com/HJK6/pentacle-chat-core), but its source
is already included in both client repositories. Do not fetch a sibling copy
or change the vendored dependency to make installation work.

## Name and bootstrap your own assistant

Follow the exact [two-phase own-assistant recipe](docs/assistant.md#bootstrap-your-own-assistant)
after ordinary daemon/provider setup. Run the checked-in wrapper with explicit
endpoint, owner-only operator credential, physical host label, name,
provider/model/effort, private workspace and instructions file. Dry-run has no
mutation. Activation uses authenticated operator spawn, returns the actual ready
backend stream/generation and writes owner-only config. The owner restarts the
same daemon/stores and verifies binding before the first input. The wrapper
never installs or restarts a service.

The name is display identity; `assistant` is the protected role and `local` is
the physical execution host label. Web/mobile use the host's reachable address
and matching role, without a legacy assistant alias or a rebuild just to set a
display title. Mic stays disabled by default; naming a typed assistant does not
customize the current fixed voice wake. Use [the process kit](process/README.md)
and [memory discovery](process/MEMORY.md) for private facts, preferences,
decisions and work; configure daemon and CLI memory roots separately.

## Contributions

PRs, feature requests, and bug reports are welcome — open an issue or pull
request at <https://github.com/HJK6/pentacle/issues>.
