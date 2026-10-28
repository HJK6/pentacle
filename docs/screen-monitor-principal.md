# Restricted screen monitor principal

The fixed `codex-reset-monitor` principal uses its own user-owned 0600 credential
file, configured for the daemon with `PENTACLE_CODEX_RESET_MONITOR_TOKEN_FILE`.
Set `PENTACLE_CODEX_RESET_MONITOR_HOST` to the single approved execution host.
An absent, invalid, shared or revoked credential fails closed; no seat is created.
Host names, paths and secret values belong in private runtime configuration.

Set `PENTACLE_CODEX_RESET_MONITOR_COMPOSITE` to the one approved assistant composite.
The principal may authenticate an RPC-only hello, then acquire, renew or release
its own screen lease on that host. Requested TTL must be an integer from 1 to
120 seconds; the default is 120. It cannot force-release another holder, read
inventory/status, publish assets, create notifications or invoke account operations.
Its only message route is a bounded reset-signal or watcher-failure tell to that
configured composite. Direct seat targets, raw aliases, other composites, arbitrary
bodies and send/urgent authority are refused. The existing composite route resolves
the live bound front desk at send time, preserving provenance and tell-id dedup. The existing lease transaction enforces host and ownership.
Other principals retain their existing permissions.

The installed CLI uses `AGENT_ORCH_STREAM_ID=codex-reset-monitor` and a private
`AGENT_ORCH_STREAM_TOKEN_FILE`. Identity is resolved before credentials; a missing
service token never falls back to an operator or seat credential. The read-only
monitor's native driver verifies its lease by renewing that same ID, so it needs
no inventory permission. A contended acquire returns `screen_held`; the monitor
skips that tick and retries at its ordinary cadence. Alerts use the same restricted principal and the existing composite tell path;
there is no phone delivery.

Provision a distinct secret through the deployment's secure store, materialize
only a 0600 runtime token file, and activate the matching daemon and CLI release.
Record artifact and runtime readbacks. Remove the token configuration or rotate
its file to revoke existing sockets; stop only the monitor's own scheduled job
when disabling the feature. Never log credentials or copy an implementation
seat token into a service environment.
