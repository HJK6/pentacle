"""Cosmo E2E isolation + static audits (unit tier): one named check per forbidden
boundary, each asserting the real sink is NOT invoked and the harness refuses
non-disposable resources, plus the production-coupling audits (import graph, no
new prod env flag) and the manifest-completeness check.  No host-private path
literal lives here."""
from __future__ import annotations

import ast
import asyncio
import tempfile
from pathlib import Path

import pytest

import consent_push
import transcribe
from blobs import DEFAULT_BLOB_ROOT
from tests.cosmo_e2e import harness as H

SERVICE_DIR = Path(__file__).resolve().parents[1]
TMPBASE = Path(tempfile.gettempdir()).resolve()


# --- F1 refusal guards: the ROOT itself + live/default paths + occupied ports - #
def test_refuses_non_disposable_root_under_home():
    # A root outside the OS disposable temp base (e.g. under $HOME) is refused.
    with pytest.raises(H.IsolationError):
        H.assert_disposable_root(Path.home() / "cosmo-e2e-should-refuse")


def test_refuses_default_blob_root_as_harness_root():
    with pytest.raises(H.IsolationError):
        H.assert_disposable_root(Path(DEFAULT_BLOB_ROOT))


def test_refuses_home_path_with_pytest_component():
    # F1 hole: a $HOME path that merely CONTAINS a ``pytest-*`` component must
    # still be refused (the old rule accepted any ``pytest``-named component).
    with pytest.raises(H.IsolationError):
        H.assert_disposable_root(Path.home() / "pytest-of-attacker" / "evil-root")


def test_constructor_refuses_non_disposable_root():
    with pytest.raises(H.IsolationError):
        H.FoundationHarness(Path.home() / "cosmo-e2e-should-refuse")


def test_constructor_refuses_protected_blob_root():
    # Pass a real protected live data root (DEFAULT_BLOB_ROOT) to the constructor.
    with pytest.raises(H.IsolationError):
        H.FoundationHarness(Path(DEFAULT_BLOB_ROOT))


def test_runner_cleans_temp_root_on_failure_after_allocation(monkeypatch):
    # F6: the run.py runner allocates a disposable temp root, then constructs the
    # harness.  A failure injected AFTER allocation but BEFORE construction must
    # still remove that exact temp root (no unprotected window).
    from tests.cosmo_e2e import run as R

    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _recording_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        created.append(Path(path).resolve())
        return path

    class _Boom:
        def __init__(self, *a, **k):
            # Raises as the first thing after the root is allocated.
            raise RuntimeError("cosmo-e2e injected failure after allocation")

    monkeypatch.setattr(R.tempfile, "mkdtemp", _recording_mkdtemp)
    monkeypatch.setattr(R.H, "FoundationHarness", _Boom)
    with pytest.raises(RuntimeError, match="after allocation"):
        asyncio.run(R._start_seed_run_teardown())
    assert created, "runner never allocated a temp root"
    for path in created:
        assert not path.exists(), f"runner leaked temp root after allocation: {path}"


def test_refuses_memory_db(tmp_path):
    with pytest.raises(H.IsolationError):
        H.assert_disposable_db(":memory:", tmp_path)


def test_refuses_production_blob_root(tmp_path):
    with pytest.raises(H.IsolationError):
        H.assert_disposable_blob_root(DEFAULT_BLOB_ROOT, tmp_path)


def test_refuses_db_outside_tmp_root(tmp_path):
    # A path under the temp base but OUTSIDE this harness root is refused
    # (relocatable: no host-private literal).
    outside = TMPBASE / "cosmo-e2e-elsewhere" / "sessions.db"
    with pytest.raises(H.IsolationError):
        H.assert_disposable_db(str(outside), tmp_path)


def test_refuses_creds_outside_tmp_root(tmp_path):
    outside = TMPBASE / "cosmo-e2e-elsewhere" / "creds.json"
    with pytest.raises(H.IsolationError):
        H.assert_disposable_creds(str(outside), tmp_path)


def test_refuses_occupied_port():
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        with pytest.raises(H.IsolationError):
            H.assert_free_port(busy)


# --- egress / real-sink audits: the real backends are NEVER invoked ---------- #
def test_transcriber_real_backend_poster_never_invoked(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real transcriber backend poster was invoked")

    monkeypatch.setattr(transcribe, "_urllib_post", _boom)

    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob:
                sha = await hz.upload_blob(mob, H.materialize_fixture("voice"))
                out = await mob.rpc("transcribe_blob", request_id="iso-tr",
                                    blob_sha=sha, mime="audio/mp4")
                assert out["type"] == "transcribe_blob.ok"
                assert len(hz.transcriber_stub.calls) == 1  # only the labelled stub ran
        finally:
            await hz.stop()

    asyncio.run(run())


def test_push_real_transport_never_invoked(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real Expo push transport (consent_push.post) was invoked")

    monkeypatch.setattr(consent_push, "post", _boom)

    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                await mob.rpc("send", request_id="r-iso", to_stream_id=H.DAFF_CHAT,
                              text="hi", msg_id="m-iso")
                did = await hz.resolved_dispatch_id("m-iso")
                await seat.publish(dispatch_id=did, reply_to_message_id="m-iso", message="reply")
                assert len(hz.push_sent) == 1  # committed reply pushed once, via the stub transport
        finally:
            await hz.stop()

    asyncio.run(run())


def test_no_fleet_spawn_or_hosts_wired(tmp_path):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            server = hz.server
            assert getattr(server, "spawnctl", None) is None       # no spawn control
            assert not getattr(server, "hosts", None)              # no peer fleet / egress
            assert getattr(server.sessions, "tmux", "sentinel") is None  # no tmux -> no pane/model
        finally:
            await hz.stop()

    asyncio.run(run())


# --- production-coupling static audits --------------------------------------- #
def _iter_production_py():
    for p in SERVICE_DIR.rglob("*.py"):
        rel = p.relative_to(SERVICE_DIR)
        if rel.parts and rel.parts[0] in {"tests", ".venv"}:
            continue
        yield p


def test_production_startup_never_imports_harness():
    offenders = []
    for path in _iter_production_py():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            if any("cosmo_e2e" in (m or "") for m in mods):
                offenders.append(str(path.relative_to(SERVICE_DIR)))
    assert offenders == [], f"production modules import the test harness: {offenders}"


def test_no_new_production_env_flag():
    for path in _iter_production_py():
        text = path.read_text()
        assert "COSMO_E2E" not in text, f"{path} references a COSMO_E2E env flag"
    harness_src = (SERVICE_DIR / "tests" / "cosmo_e2e" / "harness.py").read_text()
    tree = ast.parse(harness_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Attribute)
                        and tgt.value.attr == "environ"):
                    raise AssertionError("harness mutates os.environ")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"setenv", "putenv"}:
                raise AssertionError("harness sets a process env var")


def test_harness_uses_only_existing_daff_config_flags():
    harness_src = (SERVICE_DIR / "tests" / "cosmo_e2e" / "harness.py").read_text()
    for key in ("PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED",
                "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID",
                "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID",
                "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION"):
        assert key in harness_src
    from assistant_composite import AssistantCompositeConfig
    cfg = AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": "daff:assistant",
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": "x:y",
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": "g",
    }, name="daff", env_prefix="DAFF_")
    assert cfg.enabled and cfg.direct_primary


def test_no_host_private_home_path_literal_in_harness_files():
    """Guard the public-residue boundary at the source: no host-private home-dir
    path literal in any cosmo_e2e harness file (the gate check also enforces it).
    The token is assembled at runtime so this guard does not itself contain one."""
    token = "/" + "Users" + "/"  # assembled so the guard carries no literal
    roots = [SERVICE_DIR / "tests" / "cosmo_e2e",
             SERVICE_DIR / "tests" / "smoke" / "test_cosmo_e2e_walk.py",
             SERVICE_DIR / "tests" / "test_cosmo_e2e_cosmo_rows.py",
             SERVICE_DIR / "tests" / "test_cosmo_e2e_isolation.py"]
    files = []
    for r in roots:
        files += list(r.rglob("*.py")) if r.is_dir() else [r]
    for f in files:
        assert token not in f.read_text(), f"{f} contains a host-private home path"


# --- F5: the manifest is the shared executable source of all ten cases -------- #
#: Each manifest case_id -> the test(s) that drive it to its verify oracle.
_CASE_TESTS = {
    "text": ["tests/smoke/test_cosmo_e2e_walk.py::test_text"],
    "fail-once": ["tests/smoke/test_cosmo_e2e_walk.py::test_fail_once_dispatch_then_idempotent_publish"],
    "reconnect": ["tests/smoke/test_cosmo_e2e_walk.py::test_reconnect_idempotent_admission_and_receipt"],
    "photo": ["tests/smoke/test_cosmo_e2e_walk.py::test_photo_real_blob_roundtrip_and_publish_attachment"],
    "voice": ["tests/smoke/test_cosmo_e2e_walk.py::test_voice_real_blob_stub_transcriber"],
    "voice-fail-once": ["tests/smoke/test_cosmo_e2e_walk.py::test_voice_fail_once_then_succeeds"],
    "history": ["tests/smoke/test_cosmo_e2e_walk.py::test_history_order_and_counts"],
    "working": ["tests/smoke/test_cosmo_e2e_walk.py::test_working_true_in_flight_then_false"],
    "calendar": ["tests/test_cosmo_e2e_cosmo_rows.py::test_calendar_add_then_delete"],
    "list-undo": ["tests/test_cosmo_e2e_cosmo_rows.py::test_list_undo_window_two_items"],
}


def test_manifest_covers_all_ten_cases_with_oracles():
    manifest = H.load_manifest()
    rows = {c["case_id"]: c for c in manifest["cases"]}
    expected = {"text", "fail-once", "reconnect", "photo", "voice", "voice-fail-once",
                "history", "working", "calendar", "list-undo"}
    assert set(rows) == expected, set(rows) ^ expected
    for cid, row in rows.items():
        # Each row carries a non-vacuous verify oracle and an action.
        assert row.get("verify", "").strip(), f"{cid} has no verify oracle"
        assert row.get("action", "").strip(), f"{cid} has no action"
        assert cid in _CASE_TESTS and _CASE_TESTS[cid], f"{cid} has no covering test"


def test_media_fixtures_match_manifest_digest():
    manifest = H.load_manifest()
    for key in ("photo", "voice"):
        data = H.materialize_fixture(key)  # raises if the digest mismatches
        assert data, key
