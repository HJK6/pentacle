# Advisory test selection

From the repository root, give the actual installed component baseline and the
proposed release as full immutable commit SHAs:

```sh
python3 scripts/select_tests.py --base BASE_SHA --head HEAD_SHA --out selection.json
python3 scripts/select_tests.py --base BASE_SHA --head HEAD_SHA --out selection.json \
  --changed-input provider_config --changed-input client_versions
```

This command writes a deterministic report and runs no tests. Existing CI,
hooks, merge gates and deploy checks remain mandatory and unchanged. Every
report sets `advisory: true`, `selective_safe: false` and `tests_executed: false`.
An exit of 0 means a report was generated, including broad fallback; it is not
a test or release verdict. Invalid revisions, unreadable inputs or a malformed
map produce an `invalid` report, an error on stderr and exit 2. Argument usage
errors also exit 2, before report generation.

The release owner supplies each independently installed component's baseline.
The helper compares that exact baseline directly to HEAD; it never substitutes
a merge base or just the last PR. Invoke it for each baseline and union the
results when components differ. Source-identical releases can still select a
boundary through `client_versions`, `provider_config`, `satellite_config` or
`deploy_config`. Labels record changed inputs, not their secret values. Unknown
labels force broad fallback.

`scripts/test-selection.json` owns the four initial boundary entries: client
wire/render, provider startup, satellite transition and deploy invocation.
Entries name existing `scripts/gate-env.json` targets, selections relative to
the target's root, literal evidence files, covered behavior and concrete gaps.
The selector reads its adjacent map from the tooling checkout, so a current
map can classify historical releases. The report records that map's byte hash,
the helper's hash and the gate configuration hash read from the requested HEAD.
Preserve those bytes with the base/head identities when retaining evidence.
Test references are checked in the requested HEAD, independently of worktree
file presence. These checks establish presence, not assertion quality.

Paths use case-sensitive shell patterns (`*` includes directory separators).
Overlapping producer and consumer entries are unioned. A NUL-delimited Git
name/status diff preserves spaces, tabs and newlines and classifies both rename
endpoints. Gitlinks force fallback because their referenced repositories are
not inspected. Unknown paths, shared code/dependencies, gate tooling, map edits
and missing target files also select all four boundaries and the existing full
gate families. Missing files stay explicit and affected targets are marked
`available_in_head: false`; fallback cannot repair or run a missing test.

Documentation exemptions are restricted to `README.md`, `CHANGELOG.md`,
`LICENSE`, and Markdown below `docs/` or `process/docs/`. Shared/tooling rules
take precedence. Runtime/config files outside the map always force fallback.
A valid empty diff reports `no_changes`; an exempt-only diff reports
`docs_only`. Neither result authorizes skipping a required gate.

Coverage gaps travel with each selected boundary. In particular, root
`npm-test` covers root consumer tests and does not execute the standalone
`pentacle-chat-core/tests` suite or that package's test/typecheck scripts. No
target for that standalone suite is invented here. The map does not discover
every missing assertion or certify an installed client/provider/fleet journey.
Later coverage additions amend only their boundary entries with the map owner;
test execution and selective release enforcement are separate work.

Focused classifier and tooling regression checks use the prepared Python
environment described in [the gate runbook](E2E_HARNESS.md):

```sh
python3 -m pytest scripts/test_select_tests.py
python3 -m pytest scripts/test_gate_env.py
```

The classifier controls use real temporary Git repositories and assert report
contents and exit status, including rename/delete/gitlink and invalid-input
cases. Set `TEST_SELECTION_EVIDENCE_DIR` to retain each CLI control's exact
command, report and raw exit outside the source tree. No runtime activation is
needed for this tooling change. Rollback is a revert of the helper, map, tests
and documentation; gate enforcement has no dependency on them.
