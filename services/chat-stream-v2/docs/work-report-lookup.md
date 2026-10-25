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
