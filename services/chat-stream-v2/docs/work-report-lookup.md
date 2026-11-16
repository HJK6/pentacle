# Accepted report lookup

`agent-orch report get <report_id> --json` reads an ingested report independently
of its reporter's current session. `work.report.get` accepts only `report_id`
besides the RPC envelope and returns `report_id`, `status`, `from_stream_id`,
`provenance_qualified_spec_ids` and `created_at`. Missing rows return `not_found`;
transport or store failures return `unavailable`. Report bodies and rejection
history are never returned. A previously rejected attempt does not invalidate a
later accepted row. Consumers must check status and qualified spec themselves.

Operators and authenticated internal seats can use this read verb. An isolated
publisher uses the fixed `work-report-reader` service principal. Configure the
daemon's `PENTACLE_WORK_REPORT_READER_TOKEN_FILE` with its own private mode-0600
file owned by the daemon user. This uses the existing file-principal verifier:
the credential cannot be shared with another service, is rechecked on every
request, and removal or rotation revokes existing connections. Its only business
verb is `work.report.get`; its RPC hello cannot request a snapshot, and it
receives no broadcasts. It has no seat or operator authority.

The privileged publisher launcher provisions the same credential in root-owned
configuration and passes `PENTACLE_WORK_REPORT_READER_TOKEN` to its isolated
process with the daemon URL configured through `AGENT_ORCH_WS_URL`. Never pass
a seat token. When that environment variable is present, the CLI ignores all
inferred seat and operator credentials, including on authentication failure.
Keep credential values out of command arguments, output, reports and source.
Provisioning and runtime changes belong to the deployment owner.

The client makes one attempt with a maximum twenty-second deadline covering
connect, reply and cleanup. `--timeout` may shorten this deadline. Failure returns
a JSON `work.report.get.error` and exit 1; success returns the five metadata
fields with the usual type/request-id envelope and exit 0. Publisher activation
depends on installed lookup and a proven launcher-identity read journey.

For a publisher that cannot traverse the daemon user's home, install the
read-only consumer at `/usr/local/libexec/pentacle-work-report/agent-orch`.
The deployment owner runs the reviewed `install_fleet_spawn_tooling.py` with
`--commit <reader-release-SHA> --install-report-consumer <existing-release-dir>
--consumer-manifest-sha256 <reviewed-manifest-SHA256>` as root, using the
publisher's isolated Python. This separate mode performs no fleet or daemon
activation. Refresh it as the paired step of each subsequent CLI release.
It verifies the copied manifest/archive before importing, installs root-owned
read-only modules and bundled dependencies, probes them as the publisher, and
atomically selects `releases/<SHA>` through `active`; `previous` retains the
rollback target. The wrapper accepts only `report get`, requires its dedicated
credential and URL, and strips seat/operator variables before dispatch.

The fixed publisher launcher reads `reader.json` in that prefix before clearing
its environment. The file has exactly `token` and `ws_url`, owner root:wheel,
mode 0600, and exactly the named-user ACL `user:_triforce_memory allow read`.
Never grant primary-group access: the publisher's group may be shared. The
launcher supplies the credential, URL and an empty `AGENT_ORCH_TOKEN` after the
reset, preserving authenticated memory-principal attribution. Preserve launcher,
config and pointer preimages, and prove the actual publisher identity before
publisher activation; installation alone is not that journey.
