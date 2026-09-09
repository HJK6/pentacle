# Public governance tripwires

This repository uses a small, repository-local review convention for changes that add destructive actors or configuration knobs. The convention is intentionally independent of a shared filesystem or a particular host.

## Review entry

An artifact that adds a kill/reap actor or a `PENTACLE_*` knob carries one scope line and one ruling line:

```text
Governance scope: new kill/reap actor
Governance ruling: necessity=<why>; reused substrate=<what>; authority=<who or what approved it>
```

For unrelated changes use:

```text
Governance scope: none — no new kill/reap actor or PENTACLE_* knob
```

Compatibility-only work may use `Governance scope: subtractive/compatibility-only`. The checker validates presence and non-empty fields; it does not impose a line-count target.

```bash
python3 tools/check_governance_tripwires.py --kind review review-body.md
```

## Retro entry

Record truthful arithmetic for removed and added lines:

```text
Deletion ratio: 42 removed / 100 added = 42%
```

If nothing was removed, write:

```text
Deletion ratio: none (0 lines removed; no deletion work)
```

```bash
python3 tools/check_governance_tripwires.py --kind retro retro.md
```

The tripwire is a review aid. It does not authorize deployment, access a remote service, or infer ownership.
