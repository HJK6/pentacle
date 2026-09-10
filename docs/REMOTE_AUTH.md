# Daemon network access

The daemon defaults to loopback. Connections whose actual peer is IPv4 or IPv6
loopback can use local CLI bootstrap, including the initial spawn and one-time
seat token grant. This is a single-user local trust boundary: other processes
on that computer can use the local daemon. A claimed client name, host, or
sender field does not establish local access.

Before exposing the daemon on a LAN or VPN for mobile, provision a separate
operator credential for each UI client with `services/chat-stream-v2/operator_auth_cli.py`.
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
