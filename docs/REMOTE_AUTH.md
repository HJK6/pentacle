# Daemon network access

The daemon defaults to loopback. Connections whose actual peer is IPv4 or IPv6
loopback can use local CLI bootstrap, including the initial spawn and one-time
seat token grant. This is a single-user local trust boundary: other processes
on that computer can use the local daemon. A claimed client name, host, or
sender field does not establish local access.

Before exposing the daemon on a LAN or VPN for mobile, provision a separate
operator credential for each UI client with `services/chat-stream-v2/tools/operator_auth_cli.py`.
Desktop uses a private credential-envelope file configured with
`chatStream.tokenPath`. The UI answers the daemon's welcome challenge during
hello. Invalid or absent authentication receives `hello.error`; it does not
receive an inventory or subscribe to broadcasts.

Remote agent CLI connections authenticate with their launch-provisioned seat
token. Persistent clients, snapshots and one-shot RPCs carry that identity.
After a verified hello, the socket may omit repeated tokens; the daemon retains
only the token hash and owner and revalidates the open seat on each RPC. Closed
or replaced tokens fail. An explicitly invalid token cannot fall back to an
earlier valid identity. Existing self/parent authorization checks still apply.
Seat credentials cannot remotely grant tokens or freeze/unfreeze spawning.

A fresh operator CLI with no seat credential should run on the daemon machine
(directly or over SSH), or use the enrolled UI to start an agent. Legacy token
strings and arbitrary `from_stream_id` claims do not authenticate remote access.
Authentication errors fail promptly instead of being retried as timeouts.

Unauthenticated network routes are limited to ping, hello, enrollment redemption,
and the satellite `event.push`/`host.stats` routes. Enrollment requires its valid
one-time code; satellite routes independently verify the configured push secret
before writes. These exceptions do not grant UI subscriptions or operator authority.
The explicitly configured system-producer credential can use RPC mode, without
receiving an inventory subscription.

## Phone approval keys

Lifecycle approval is separate from operator socket authentication. An operator-confirmed P-256 key signs the daemon's stored, expiring challenge. The daemon proves possession of that enrolled key, not remote hardware or biometric attestation. The iPhone implementation uses Secure Enclave with `biometryCurrentSet` and `privateKeyUsage`, accessible only with a device passcode set on that device. It offers no passcode fallback; changing enrolled Face ID invalidates the key.

From a loopback daemon-host shell, run `agent-orch consent-key enroll-code`. In the already operator-enrolled Pentacle iPhone app, open Settings → Approval key and enter that one-use code. Face ID signs the enrolment transcript. Read the displayed fingerprint and type `agent-orch consent-key confirm <fingerprint>` on the same host. Until confirmation the key cannot approve. A replacement retires the previous active key only when confirmed. Codes and pending keys expire after ten minutes. The daemon stores code hashes, never the plaintext code.

`agent-orch consent-key list` inspects keys; `agent-orch consent-key revoke <fingerprint>` disables a lost or invalidated key. Also revoke a lost phone's operator credential using the existing operator-auth CLI. Every consent transition reads current credential and key state; cached socket authentication is insufficient. Loss of an approval key does not revoke an existing lifecycle grant.

For urgent authority reduction without a phone, use `agent-orch lifecycle revoke --emergency --reason <why>` on the daemon host. Admission requires the actual loopback peer and the daemon-user-only `~/.config/pentacle-stream/local-admin.token` (0600). The CLI reads this token without displaying it. OS user, PID and executable in the audit are explicitly caller claims. There is no emergency designation or agent self-enrolment. The daemon-user shell remains the existing trust root; this mechanism does not defend against daemon-host compromise.
