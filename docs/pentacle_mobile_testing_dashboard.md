# Synthetic client-testing dashboard

This dashboard is a provider-free example of a bounded QA surface. It consumes a local schema-v1 envelope and renders panel data as text; it does not install packages, contact a dashboard hub, or perform rollback operations.

## Envelope

The resolver accepts `updated_at`, `server_received_at`, `freshness_ttl_sec`, and a `data.panels` object. The public fixture uses these panels:

```text
now_running
latest_gate_runs
whats_left
time_estimates
per_test_analytics
recent_closures
```

Each panel has a status of `ok`, `partial`, or `unavailable`. Transport staleness and data staleness are reported separately. An unavailable panel renders `UNKNOWN — source unreadable`; it never infers that a test passed or failed.

Payload-derived values are inserted as DOM text. Apply independent caps to panel rows, gate runs, test identities, and closure entries. Reject malformed timestamps and invalid panel shapes.

## Validation

Use unit tests for valid, stale, disconnected, malformed, and partially unavailable envelopes. The fixture runner should use a generated timestamp, a bounded synthetic label, and an explicit temporary output directory.

The dashboard can be registered in the ordinary renderer registry, but its data source is a local adapter. A public package must not include private package-install, remote transport, promotion, or application-swap instructions.
