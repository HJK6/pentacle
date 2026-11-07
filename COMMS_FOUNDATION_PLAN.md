# Web communications foundation

This document describes a small, public architecture for a browser client that
exchanges chat events with an optional local stream service. It is intentionally
repository-neutral: adapters may be replaced without changing the renderer.

## Goals

- Keep Pentacle Web and the stream service behind a documented message boundary.
- Make local development deterministic and easy to tear down.
- Keep provider-specific or environment-specific integrations outside the core UI.
- Preserve event ordering, reconnect behavior, and user-visible delivery states.

## Module boundaries

The stream service owns transport, session state, event sequencing, and lifecycle.
The Node web host owns terminal attachments, the daemon connection and the
per-browser `window.cc` bridge. The renderer owns presentation and input state.

A service implementation may split its internal class into focused mixins:

| Concern | Responsibility |
| --- | --- |
| delivery | send, tell, queue, and reconnect result handling |
| lifecycle | close, cleanup, idle detection, and shutdown |
| startup | spawn readiness, health checks, and recovery |

The shared transport schema remains the compatibility boundary. Changes to that
schema should include fixtures for both a fresh connection and a reconnect.

## Local development

Use the owned browser gate described in [the public harness](docs/E2E_HARNESS.md).
It runs the web host and disposable daemon with synthetic sessions and owned
cleanup. External profiles require separate resource authority. Teardown must
address only resources acquired by that invocation, never an unscoped process kill.

## Testing invariants

- A send is either acknowledged once or remains retryable.
- Reconnect replay is idempotent by event identity.
- A closed session cannot receive new events.
- Local fixtures use synthetic hosts, timestamps, and message bodies.
- Optional adapters fail closed when their configuration is absent.

## Extension guidance

Add a provider, storage backend, or display adapter behind a small interface.
Document the data it accepts, the data it emits, and whether it persists data.
The browser bundle should not contain credentials, private fleet configuration,
or release commands.
