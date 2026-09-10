# Set up Pentacle with a coding agent

Give a capable coding agent, such as Fable or Astra, this file's path and ask it
to complete the setup. The agent should read the linked instructions, inspect
the computer, carry out the steps and verify the result. You do not need a
separate chat-core checkout.

## Before starting

Choose desktop, mobile, or both. The daemon runs on the computer where your
coding agents work; the desktop and phone are clients of that daemon. Start
with one host named `local` and one working provider. Add other hosts later.

The local desktop recipe supports macOS and Linux. An iPhone build needs a Mac
with Xcode. Use an existing authenticated Claude or Codex CLI, or let the agent
install/configure one and request the account login when needed. Phone signing,
trust and unlock actions may require the owner. These are setup prerequisites,
not tasks the agent should pretend it completed.

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
   the Electron desktop is optional.
3. **Configure one consistent host.** Use `--local-host local`, `local` in client
   host lists, the real provider executable paths, and the real transcript
   directory. Store the database/config/credentials outside the public repo.
   Start the documented daemon command in a persistent terminal or service and
   record how to stop and restart it.
4. **Set up the desktop if requested.** Issue its credential and launch it using
   the README commands. Keep `features.chatUi: false`: structured desktop Chat
   is experimental. The normal session surface is the terminal.
5. **Set up mobile if requested.** Clone
   [pentacle-mobile](https://github.com/HJK6/pentacle-mobile), then follow its
   [mobile setup guide](https://github.com/HJK6/pentacle-mobile/blob/main/docs/FRIEND_SETUP.md).
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

## If a step fails

Use the error to narrow the next action: a refused socket needs a running,
reachable daemon; an authentication error needs the matching client credential;
an unavailable provider needs a working provider CLI and login. A physical
phone's `localhost` is the phone itself. Check these before rebuilding clients.

The shared library is public at
[pentacle-chat-core](https://github.com/HJK6/pentacle-chat-core), but its source
is already included in both client repositories. Do not fetch a sibling copy
or change the vendored dependency to make installation work.
