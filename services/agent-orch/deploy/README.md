# agent-orch deploy templates

Per-host sshd_config drop-in that lets a Pentacle-spawned session's leader stream id and private token-file path propagate across a Pentacle-internal SSH hop. The CLI side is `agent-orch ssh`, which prepends `-o SendEnv=PENTACLE_STREAM_ID AGENT_ORCH_STREAM_ID AGENT_ORCH_STREAM_TOKEN_FILE AGENT_ORCH_STREAM_TOKEN` to every ssh invocation; the sshd side (this drop-in) must accept those env names or sshd will drop them on receipt. The legacy token name remains accepted for pre-file sessions. Together they let direct `agent-orch` verbs on the remote hop assert the source-host stream identity under ownership-token enforcement, so hidden cross-host sub-agents survive SSH disconnects without `--handoff`.

The propagation list is `PENTACLE_STREAM_ID`, `AGENT_ORCH_STREAM_ID`, and `AGENT_ORCH_STREAM_TOKEN_FILE`; the legacy `AGENT_ORCH_STREAM_TOKEN` remains accepted for pre-file sessions. The v2 spawn path stages the secret in the mode-0600 file named by `AGENT_ORCH_STREAM_TOKEN_FILE`, so only the non-secret path crosses SSH. The CLI mirrors this list via `SSH_SEND_ENV_NAMES` in `agent_orch/cli.py`. **A host whose `AcceptEnv` drop-in predates the file path will silently drop `AGENT_ORCH_STREAM_TOKEN_FILE`, so cross-host spawns from it cannot self-identify — redeploy this drop-in on every host before enabling `PENTACLE_STREAM_OWNERSHIP_MODE=enforce`.**

## Per-host install

Commands below assume `cd <pentacle-checkout>/services/agent-orch/` so the relative `deploy/sshd_config.d/publicdash-leader-env.conf` path resolves. If you run from the repo root, prefix with `services/agent-orch/` instead.

The default deployment path is `/etc/ssh/sshd_config.d/publicdash-leader-env.conf` when `/etc/ssh/sshd_config` contains an `Include /etc/ssh/sshd_config.d/*` directive. Verify that before deploying:

```bash
grep -i 'include.*sshd_config.d' /etc/ssh/sshd_config
```

### macOS hosts (hosta, hostb, hostd)

```bash
sudo cp deploy/sshd_config.d/publicdash-leader-env.conf /etc/ssh/sshd_config.d/
sudo sshd -t
sudo launchctl kickstart -k system/com.openssh.sshd
```

### Linux / WSL hosts (hostc)

If the `Include` directive is present:

```bash
sudo cp deploy/sshd_config.d/publicdash-leader-env.conf /etc/ssh/sshd_config.d/
```

If it's absent, append the `AcceptEnv` line directly to `/etc/ssh/sshd_config` inside a clearly fenced comment block (the spec-marker comment in the drop-in file is the recommended fence) so a later rollback can find and remove it.

Validate, then reload. The service name varies by distro:

```bash
sudo sshd -t
sudo systemctl status sshd   # check which unit exists first
sudo systemctl status ssh
sudo systemctl reload sshd   # Ubuntu, Debian (some)
# OR
sudo systemctl reload ssh    # Debian (older), some WSL distros
# WSL fallback when systemd isn't init:
sudo service ssh reload
```

## Verification

After the reload, confirm the active sshd config accepts the named env vars:

```bash
sudo sshd -T 2>/dev/null | grep -i acceptenv
```

`PENTACLE_STREAM_ID`, `AGENT_ORCH_STREAM_ID`, and `AGENT_ORCH_STREAM_TOKEN_FILE` must all appear in the output. End-to-end check from a Pentacle-spawned session on a peer host (verify the path, not the secret contents):

```bash
agent-orch ssh <this-host> bash -c 'test -n "$PENTACLE_STREAM_ID" -a -n "$AGENT_ORCH_STREAM_ID" -a -f "$AGENT_ORCH_STREAM_TOKEN_FILE" -a "$(stat -f %Lp "$AGENT_ORCH_STREAM_TOKEN_FILE" 2>/dev/null || stat -c %a "$AGENT_ORCH_STREAM_TOKEN_FILE")" = 600'
```

The command should exit successfully on the remote side. On Linux, replace the macOS `stat -f %Lp` branch if the host's `stat` implementation differs.

## Rollback

```bash
sudo rm /etc/ssh/sshd_config.d/publicdash-leader-env.conf
sudo sshd -t
sudo systemctl reload sshd   # or the host's equivalent reload command
```

If the AcceptEnv line was appended directly to `/etc/ssh/sshd_config` instead of dropped in, locate the spec-marker comment block (`# Pentacle leader-stream env propagation (spec_pentacle_agent_orch_leader_auto_bind_2026_05_18)`) and remove the fenced section before reloading.

## Rollout ordering and backward-compatibility

Pre-rollout sessions on this host — those spawned before the sshd reload, or before the `chat_streamd` build that injects `PENTACLE_STREAM_ID` + `AGENT_ORCH_STREAM_ID` at spawn time — fall back to tmux shell-leader discovery and continue to function. The rollout is additive at the SSH/CLI layer and does not break in-flight sessions; hosts can be deployed incrementally without coordinating across the fleet.

## Safety

Keep an existing SSH session open during the reload as a fallback. If `sshd -t` succeeds but the reload still locks you out (rare, usually a PAM/auth misconfig the drop-in didn't touch), the already-open session can restore the previous file and reload again.
