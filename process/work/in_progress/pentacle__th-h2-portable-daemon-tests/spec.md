# TH-H2 portable daemon tests

Packet: TH-v1 rev 0.4, H2. Base: 586942610bdaf54c079b57d3324bc87042eb6024.
Branch: dot/th-h2-portable-daemon-tests. Owner: dot.

Scope: test-only bounded capability probes, required isolated-interpreter dependency preflight, strict fleet mode, synthetic fixture counterparts, and fixture cleanup/isolation. No product source, dependencies, workflows, pins, or existing assertions are changed. The tracked unconditional socket-race skip remains byte-for-byte unchanged.

Acceptance: distinguish absent capabilities from probe errors and broken dependencies; preserve -I; bound changed tests with existing pytest-timeout; exercise forced absence/error and strict behavior; report capability-specific skip identities including xdist workers; pin every synthetic fixture; list every managed test and its classification.

Gate: install existing requirements into the exact interpreter's venv, run the changed-surface tests and compileall at the final head. Full-capability strict execution remains fleet-owned. Keep this branch on its original base; the fleet integrates conflicts.
