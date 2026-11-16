# Test prompt profiles

All test, smoke, canary, native qualification, and proof actors resolve
`profiles.test_prompt` from `services/_shared/spawn_defaults.json`. A private
`spawn_defaults.local.json` beside it may override complete provider pairs:

```json
{"schema_version": 1, "profiles": {"test_prompt": {
  "codex": {"model": "gpt-6-luna", "effort": "low"},
  "claude": {"model": "claude-haiku-5-5", "effort": "low"}
}}}
```

The shipped settings use Haiku/low, Luna/low, and Flash Lite/default. Gemini
tests are not activated by this configuration. Cursor tests are disabled with
`null` until a verified lowest-tier tuple is supported. Missing, partial,
unknown, disabled, or unsupported entries refuse before a prompt; there is no
work-profile fallback. An invalid test policy appears as `test_prompt_error`
in the catalog while ordinary work profiles remain available. Picker visibility
does not determine native or explicit test tuple support. Ordinary work defaults and handoff inheritance are unchanged.

`agent-orch profiles` exposes `profiles.test_prompt` and `test_prompt_profile`.
The latter carries canonical provider pairs, their digest, settings byte hashes,
and the resolver hash. Settings are read afresh for the next test run. One run
freezes that snapshot; settings drift before a later cell refuses the cell.
For an ad-hoc proof, read the profile and pass its explicit `--model` and
`--effort` through the existing spawn command. A delayed one-shot proof freezes
those values at scheduling; the recurring fleet smoke resolves each occurrence.

The fleet runner supports `--dry-run` for both full matrices and explicit
URL/host canaries, without a connection or model call. Full-matrix dry plans
require the existing machine file and exclusions. The emitted cells include
model, effort, account selection, and profile digest. Native qualification keeps
its binding/config pins; a provider-tuple file must match the test profile.
Native operation receipts retain the profile used before execution, including
on resume. Native host `cells.*.model/effort` no longer select a test model.

Forward deployment preparations seal test profile JSON, per-cell tuples and
settings/resolver identity. `--test-profile-snapshot PATH` accepts that derived
artifact in the fleet runner's dry and live modes. The deployer verifies it;
each forward cell checks the live catalog and requested/effective tuple before
cleanup. Missing/invalid target settings and drift still refuse before inference.

Daemon rollback sends no model input. A known-good prior needs its accepted
production forward qualification and exact source gate, but no test-profile API,
sealed runner or model smoke. After restoring the owned source/settings/policy/
helper bindings and launchd preimages, recovery proves fresh boot/PID/birth/SHA,
account/catalog readback and one raw zero-input spawn-to-ready-and-owned-close
per included host/provider/account binding. Fresh strict helper/native dispatch
proof, attributed slow-consumer checks and fenced cleanup remain mandatory.
The CLI's baseline injection and fleet smoke's marker-sending `promptless` mode
are not zero-input probes.

Rollback records restoration acceptance separately from model canaries not run
by design. It does not certify inference auth/quota/availability or reply delivery,
and never implies fresh full-fleet model acceptance. Automatic recovery preserves
the failed forward outcome. Old receipts stay immutable under their pinned
controller and original policy; new coordinated receipts use schema v2 and
unsupported receipts fail closed. No new flag or model exception is required.

The existing immutable installer distributes and verifies matching shared
settings, including private overlays. Coordinated transactions preserve overlay
bytes and permissions with the other preimages and restore them on rollback.
Unrelated overlay fields remain intact. Helper settings are verified beside the
existing artifact identity; that identity algorithm is unchanged.

Retained UI walks select the configured tuple through real controls before a
prompt and refuse when unavailable. Their missing legacy harness is not restored.
Synthetic tuple-validation fixtures and historical receipts remain synthetic
or historical; they do not authorize a current live model choice.

Scheduled smoke keeps its existing timer and per-occurrence approval gate.
An unapproved occurrence expires under that gate; no catch-up is introduced.
A deployment's source checks alone do not establish installed runtime acceptance.

Satellite preflight receipts include the same `test_profile` snapshot for their
exact target. Pin-window canaries reuse that snapshot across the included hosts;
a receipt with missing or changed settings refuses before the pin changes.
