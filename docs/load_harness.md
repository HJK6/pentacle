# Local load and soak harness

The public load harness is provider-free. It runs a local daemon with synthetic sessions, bounded payloads, and temporary stores so repeated event delivery can be measured without a remote database or event-push credential.

## Presets

Use the repository's soak gate when available:

```bash
SOAK_TIER=short services/chat-stream-v2/tools/soak_gate.sh
```

`short` is for local iteration. A longer preset may be used in a controlled CI environment, but the test must still use a disposable store and a fixed candidate identifier.

## Fixture workload

Generate a fixed number of sessions, send bounded prompts at a fixed rate, and collect counts for accepted, rejected, duplicated, and delayed frames. Use a deterministic seed and a local fake provider. Do not pass a token, absolute private path, or remote stream id on the command line.

Evidence should be written below an explicit temporary directory selected by the test runner. Each record contains the workload parameters, elapsed time, error counts, and a verdict; it does not contain transcripts.

## Observability

Prefer the daemon's documented health and metrics interfaces. If a metric is not part of the public interface, treat it as unavailable rather than scraping a private deployment. The harness should fail cleanly when an optional metrics endpoint is absent.

## Safety limits

Cap concurrent sessions, event size, run time, and retained evidence. Stop on unexpected network access or a non-loopback endpoint. Delete the temporary database and evidence directory after the run.
