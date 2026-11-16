# Cosmo E2E disposable Foundation harness

Test-only, loopback, fully-disposable integration harness that drives the **real**
public Foundation server (chat-stream-v2) end-to-end for Cosmo's six-screen E2E
sim walks, plus the real disposable Cosmo server for calendar/list. It is **never**
imported by production startup (`main.py`/`launch.py`); it adds no production code
and no new production env flag.

Spec & acceptance: `work/<status>/pentacle__cosmo_e2e_foundation_harness_2026_10/spec.md`
in the shared memory repo (advisor contract ee99c47e). This doc records the
as-shipped design; the spec owns the full acceptance contract.

## Layout
- `tests/cosmo_e2e/harness.py` — the composed builder (`FoundationHarness`) + isolation guards + scripted responder/stubs + wire-auth clients.
- `tests/cosmo_e2e/cases.json` — the ONE shared case manifest (10 rows + schema + fixture digests); the executable source for the walk.
- `tests/cosmo_e2e/conftest.py` — path setup: inserts the Cosmo checkout (`COSMO_SERVER_DIR`, default the sibling cosmo worktree) onto `sys.path` so `cosmo_server` is importable. The Cosmo row tests themselves catch `ImportError` and `pytest.skip` with an explicit reason when `cosmo_server` is absent (so public CI stays green without a silent pass).
- `tests/cosmo_e2e/run.py` — reproducible start/seed/run/teardown + emits receipt A.
- `tests/cosmo_e2e/fixtures/{photo.png,voice.m4a}` — deterministic media fixtures (sha256 recorded in `cases.json`).
- `tests/smoke/test_cosmo_e2e_walk.py` — Foundation walk (smoke tier): wire-auth cases, fail-once / reconnect / rejected-before-admission, three applied-mutation negative controls, cleanup.
- `tests/test_cosmo_e2e_cosmo_rows.py` — Cosmo calendar + two-item list-undo window (unit tier, importorskip-guarded).
- `tests/test_cosmo_e2e_isolation.py` — per-boundary isolation + static audits (unit tier).

## Architecture
Two real servers, both stood up disposably; one shared manifest drives both.

**Foundation half** — constructs the real `server.Server(host=127.0.0.1, port=0, ...)`
with a disposable `Store`, `Sessions`, `BlobStore` and scoped `operator_auth`, plus a
daff `AssistantComposite` (direct-primary) configured via the PRE-EXISTING
`PENTACLE_ASSISTANT_DAFF_*` env keys (set only in-process; not a new flag). The only
scripted boundaries are the backend `dispatch` callback (also the fail-once seam) and
the external transcriber poster + Cosmo push transport/table (stubbed, labelled). The
scripted responder publishes ONLY through the authorized `assistant.publish` verb over
a real bound WebSocket; functional cases authenticate via the real `auth_v2`
challenge/proof (mobile) or a `stream_token` hello (backend seat). Blob upload/download
is REAL with fixture bytes.

**Cosmo half** — imports the real `cosmo_server.app.create_app(tmp/"cosmo.db",
tokens=…, weather_enabled=False, clock=<advanceable>)` and drives it via `TestClient`;
the advanceable clock proves the server-owned undo window (HTTP 410 after `WINDOW`).

## Isolation guarantees (enforced + tested)
- Loopback-only port, owned disposable DB/blob/config roots, disposable credentials.
- `assert_disposable_root` accepts a root ONLY under the OS temp base or an explicitly
  designated gate/test base (`V2_GATE_EVIDENCE_DIR` / `COSMO_E2E_DISPOSABLE_BASE`), and
  refuses live/default roots (`DEFAULT_BLOB_ROOT`, configured data roots). (A path is NOT
  disposable merely because a component is named `pytest`.) The DB-path guard
  `assert_disposable_db` separately rejects the `:memory:` sentinel.
- No external egress / fleet / spawn / model / real transcriber / real APNs — each is a
  stub whose real sink's call-count is asserted 0, or is simply never wired.
- Static audits: `main`/`launch` do not import the harness; no new `PENTACLE_*` prod flag.
- Cleanup is proven on normal AND failure paths (incl. a failure between temp-root
  allocation and construction) — disposable root removed, port released.

## Run it
From `services/chat-stream-v2` under the dev `.venv` (`env -u TMUX`):
- Foundation walk: `./.venv/bin/python -m pytest tests/smoke/test_cosmo_e2e_walk.py`
- Cosmo rows (needs `cosmo_server` importable — `pip install -e <cosmo>/server` into the venv, or set `COSMO_SERVER_DIR`): `./.venv/bin/python -m pytest tests/test_cosmo_e2e_cosmo_rows.py`
- Isolation: `./.venv/bin/python -m pytest tests/test_cosmo_e2e_isolation.py`
- Gates: `./.venv/bin/python tools/run_gate.py unit` and `… smoke` (or `… merge`, the CI gate).
- Receipt A: `PYTHONPATH="$PWD:$PWD/..:$PWD/../_shared" ./.venv/bin/python tests/cosmo_e2e/run.py --gate-evidence-dir <dir with {unit,smoke}.junit.xml> --out <receipt.json>`

## Handoff
The Cosmo lead owns the app build/profiles, Maestro inputs, fixtures/seed contract and
runtime/visual acceptance. Receipt A (infra proof) binds the foundation and candidate
SHAs plus the required Cosmo baseline SHA (`09ffbf6b`) with an ancestry/presence check
against the discovered Cosmo checkout (it records the required baseline, not the
discovered checkout HEAD), the gate digests, and the disposable root + cleanup, with app
fields `pending (Cosmo lead)`; the
runnable handoff receipt (B) — app build/profile, Maestro mapping, availability — is
completed with the Cosmo lead. The manifest's `maestro_input` column is theirs to fill.
