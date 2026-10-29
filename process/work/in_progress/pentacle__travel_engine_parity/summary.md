---
id: work_pentacle__travel_engine_parity
title: Travel engine parity in Pentacle
type: work
status: in_progress
canonical: false
created_at: '2026-09-28'
updated_at: '2026-09-28'
source_path: work/in_progress/pentacle__travel_engine_parity/summary.md
machine: local
owner: codex
tags:
- pentacle
summary: Expose the full standalone travel engine inside Pentacle, including complete
  airport autocomplete, without maintaining a second implementation.
related:
- spec_pentacle__travel_engine_parity
---

# Travel engine parity in Pentacle — quick state

**Goal** — Embed the standalone Travel Engine UI in Pentacle and expose every airport-code prefix match in its picker.

**Status** — implementation, validation and independent code review are complete; the implementation review returned ACCEPT. The work item remains `in_progress` because no Git publication or runtime activation was performed. Pentacle now embeds the original Travel Engine UI behind its existing Dashboards opt-in, and the original airport matcher returns every code-prefix result ahead of bounded text suggestions.

**Evidence** — the original matcher reproduction showed 352 airport codes for `a` but returned no airport codes, and truncated 21 `ab` matches to 12: `_artifacts/autocomplete_baseline.txt`. The isolated cross-origin browser fixture passes 36 checks over the original page and adapter, synthetic same-origin APIs, readiness/retry, airport selection, all six source categories, Points/Dollars, filters, layout, detail, share, Wallet, Vault, booking review and external-link dispatch; it observed no purchase confirmation: `_artifacts/embed_fixture_green.txt`. Dataset-derived checks cover all 6,770 distinct 1–3 character prefixes and compare airport and metro rows independently: `_artifacts/standalone_test_ui.txt`. An initial stricter assertion exposed a jsdom/Node cross-realm array comparison in the test harness; the direct row comparison was equal, the assertion was normalized, and the rerun passed: `_artifacts/standalone_test_ui_kind_oracle_failure.txt`. Pentacle focused tests pass 51/51 and the full suite passes 987 with one skipped: `_artifacts/pentacle_focused_tests.txt`, `_artifacts/pentacle_npm_test.txt`. The standalone backend suite passes 318 tests: `_artifacts/standalone_make_test.txt`. Browser build: `_artifacts/pentacle_build_web.txt`; its served page loads the copied `dashboards-travel-engine.js` adapter. Success and forced-failure test-ui cleanup evidence: `_artifacts/standalone_test_ui.txt`, `_artifacts/test_ui_cleanup_forced_failure.txt`. Toolchain, source revisions, dataset/harness hashes and synthetic fixture configuration: `_artifacts/validation_environment.txt`.

The independent implementation review returned ACCEPT; its receipt is `_artifacts/implementation_qa_accept.txt`. The sibling virtualenv initially lacked `pytest-asyncio`, so three existing async tests could not run. Version 1.4.0 was installed in the ignored local `.venv` only; rerunning `make test` passed, and no requirements/source file was changed for that pre-existing harness setup gap. `jsdom` is also installed only in ignored sibling `node_modules` for the autocomplete test. The requested `agent-orch` status-card CLI is unavailable (`command not found`), so this summary and the work item frontmatter are the durable checkpoint. The root checkout's pre-existing untracked `services/dashboard-hub/` and `services/lews/` trees remain outside this work.

**Next action** — when activated, set the private `travelEngine.url`, enable `features.dashboards`, and deploy/restart the separately operated Travel Engine with the updated `web/index.html`; then verify the installed service and Pentacle process. The service at the default local URL was offline during this work, and starting its FastAPI app was avoided because startup auto-unlocks the remembered vault key and launches the persistent provider browser manager. No commit, push, or runtime activation is claimed. The source and generated browser adapter are ready in the current worktrees.

**Evidence location** — raw validation receipts and QA receipts are under this work item's `_artifacts/`; the accepted spec is `spec.md`.
