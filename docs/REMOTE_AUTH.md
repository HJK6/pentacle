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
earlier valid identity. A verified seat has operator-equivalent RPC authority:
agents are the operator interface for headless/CLI installations, including
role changes, token grants, and spawn freeze/unfreeze controls. A separate
human-authentication proof is not required after seat-token verification.

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

Lifecycle approval is separate from operator socket authentication. A registered P-256 key signs the daemon's stored challenge; this proves key possession, not remote Face ID or hardware attestation. Each iPhone signature uses the existing Face-ID-only Secure Enclave policy. Sign-in creates no Approval key, offer or lifecycle grant.

Choose a mobile credential explicitly with `agent-orch consent-key devices`, then `agent-orch consent-key offer --credential <id>`. The CLI uses the actual loopback peer and daemon-user local-admin token. The host web action **Set up Approval key on phone** instead requires its fresh operator credential. Seats, services and phones cannot issue offers. The phone must have authenticated support for the new protocol; a supported offline phone also needs its credential-bound push registration. An old app must update; an existing active key survives without another setup.

The phone receives a setup notification, opens that exact request, and taps **Set up with Face ID** or **Not now**. Setup has no code, fingerprint readback or Settings section. Successful acceptance activates the new key atomically and retires only the expected prior key on that credential. Until success, an old active key remains usable. Offer status and exact receipts are scoped to the target phone; authorized host progress is separate. `agent-orch consent-key status <offer-id>` reads host progress and push degradation. A host may cancel a pending offer, then send a new one.

Offers and approval intents last 24 hours. Only an explicit user open creates the signing challenge, lasting at most 120 seconds. Reading, receiving, reconnecting or background delivery starts no signing timer. Duplicate open returns the same live challenge; an explicit reopen after expiry replaces its nonce/id. Each audience phone opens a challenge for its own key. Only one parent approval can apply the immutable lifecycle action. Exact signed retries return the stored receipt, with fresh auth, and cannot reactivate a revoked key. Native key metadata and signed tuples persist before send; reconnect never signs or submits automatically.

The [shared transcript fixture](consent-offer-transcript.json) specifies enrollment framing and integer expiry encoding. Its domain differs from the retired enrollment protocol. Lifecycle challenge framing remains unchanged. Migration disables old pending code/key ceremonies and mint-time challenges while retaining existing active keys and historical audit rows. Retired prepare/enroll/confirm routes fail through the normal unsupported path.

Use `agent-orch consent-key list` and `agent-orch consent-key revoke <fingerprint>` from the daemon-host shell to review or revoke a lost, invalidated or conflicting setup key. A security notification links to the exact setup offer for review and this recovery route. Also revoke a lost phone's operator credential through the existing operator-auth CLI. A key revoke does not revoke an existing lifecycle grant. Send a fresh targeted offer for replacement; no Settings recovery flow is required.

For urgent authority reduction, `agent-orch lifecycle revoke --emergency --reason <why>` retains actual loopback plus the daemon-user-only local-admin token (0600). The CLI never displays that token. There is no emergency designation. The daemon-user shell remains the trust root; this does not defend against host compromise or prove which physical device owns a copied operator credential.

Consent OS delivery uses Expo→APNs. Configure `EXPO_PROJECT_ID` on the sender to the app's EAS UUID; `EXPO_ACCESS_TOKEN` is optional unless that project's enhanced push security requires it. The app's `apple.pushEnvironment` must match its signed `aps-environment` entitlement (development by default, preserving the existing installed signing profile). Registration binds host, fresh mobile credential, token, project and environment; legacy unbound DynamoDB tokens are ineligible. Rotation/unregister affect only their authenticated owner. A token is routing data, not authority; another credential cannot take an existing binding. Account switching unregisters on the old socket before replacing it; an unavailable old socket can leave a binding that requires host recovery rather than token takeover.

Committed requests enqueue bounded durable transport work. Dispatch revalidates authority before sending outside the authority lock. Expo tickets are polled for receipts after fifteen minutes, with separate bounded retries; process loss cannot restart an unlimited send loop. An old invalid-token receipt cannot remove a rotated registration. Missing permission, registration, sender/project or provider failure is reported as degraded status. Push contains only generic copy and opaque host/request ids; taps are untrusted navigation hints and must authenticate to read/open. A provider receipt proves provider handoff, not physical OS display. In-app automatic presentation remains a fallback.
