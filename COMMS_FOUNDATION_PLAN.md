# Desktop communications foundation

This document describes a small, public architecture for a desktop client that
exchanges chat events with an optional local stream service. It is intentionally
repository-neutral: adapters may be replaced without changing the renderer.

## Goals

- Keep the Electron client and stream service behind a documented message boundary.
- Make local development deterministic and easy to tear down.
- Keep provider-specific or environment-specific integrations outside the core UI.
- Preserve event ordering, reconnect behavior, and user-visible delivery states.

## Module boundaries

The stream service owns transport, session state, event sequencing, and lifecycle.
The desktop main process owns the local IPC bridge and window lifecycle. The
renderer owns presentation and input state.

A service implementation may split its internal class into focused mixins:

| Concern | Responsibility |
| --- | --- |
| delivery | send, tell, queue, and reconnect result handling |
| lifecycle | close, cleanup, idle detection, and shutdown |
| startup | spawn readiness, health checks, and recovery |

The shared transport schema remains the compatibility boundary. Changes to that
schema should include fixtures for both a fresh connection and a reconnect.

## Local development

A public adapter should bind to loopback by default and store its state below a
throwaway directory. A typical smoke test is:

```bash
bash scripts/launch-local-service.sh --port 7797
python3 scripts/smoke-check.py --port 7797
bash scripts/teardown-local-service.sh --port 7797
```

Each invocation owns one state directory and one process identifier. Teardown
must address only that invocation and must not use an unscoped process kill.

## Testing invariants

- A send is either acknowledged once or remains retryable.
- Reconnect replay is idempotent by event identity.
- A closed session cannot receive new events.
- Local fixtures use synthetic hosts, timestamps, and message bodies.
- Optional adapters fail closed when their configuration is absent.

## Extension guidance

Add a provider, storage backend, or display adapter behind a small interface.
Document the data it accepts, the data it emits, and whether it persists data.
The desktop core should not contain credentials, fleet routing, or release
commands.
