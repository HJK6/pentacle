"""Offline provider boundaries. Never executes an installed Gemini CLI."""
import asyncio
import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import time

import pytest

import launch
from machines import MachineConfig
from _shared import spawn_profiles
from gemini_jsonl_norm import normalize_gemini_jsonl_records
from ingest import _identity_key
from provider_accounts import transcript_allowed
from boot_ready import submission_proven_after, gemini_prompt_ready

SID = "a1b2c3d4-1111-2222-3333-444455556666"
OTHER = "a1b2c3d4-9999-2222-3333-444455556666"
FIXTURES = Path(__file__).parent / "fixtures"


def test_machine_positional_compatibility_and_configured_binary():
    cfg = MachineConfig("local", None, "tmux", "/tools/claude", "/tools/codex", "/projects", "/work")
    assert (cfg.codex_bin, cfg.projects_root, cfg.cwd) == ("/tools/codex", "/projects", "/work")
    cfg = MachineConfig("local", None, claude_bin="/tools/claude", codex_bin="/tools/codex",
                        projects_root="/projects", cwd="/work", gemini_bin="/tools/custom-gemini")
    assert launch.machine_from_config(cfg, provider="gemini").gemini_bin == "/tools/custom-gemini"


def test_remote_gemini_refuses_until_native_ingest_is_supported():
    cfg = MachineConfig("remote", "example.invalid", cwd="/work", projects_root="/projects",
                        gemini_bin="/tools/gemini")
    with pytest.raises(ValueError, match="gemini_remote_unsupported"):
        launch.machine_from_config(cfg, provider="gemini")


def test_legacy_two_provider_config_and_picker_stay_compatible(monkeypatch, tmp_path):
    config = json.loads(spawn_profiles.SPAWN_DEFAULTS_PATH.read_text())
    config["providers"].pop("gemini", None)
    path = tmp_path / "defaults.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(spawn_profiles, "SPAWN_DEFAULTS_PATH", path)
    spawn_profiles.load_spawn_config.cache_clear()
    try:
        cat = spawn_profiles.catalog()
        assert set(cat["profiles"]["desktop_manual"]) == {"claude", "codex"}
        assert set(cat["available_models"]) == {"claude", "codex"}
        assert spawn_profiles.resolve_spawn(provider="codex")["model"] == config["providers"]["codex"]["model"]
        assert spawn_profiles.resolve_spawn(provider="claude")["model"] == config["providers"]["claude"]["model"]
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


def test_gemini_effort_is_cli_default_only():
    assert spawn_profiles.resolve_spawn(provider="gemini")["effort"] == "default"
    for model in spawn_profiles.MODELS["gemini"]:
        assert spawn_profiles.MODELS["gemini"][model]["efforts"] == ("default",)
        with pytest.raises(spawn_profiles.SpawnProfileError):
            spawn_profiles.resolve_spawn(provider="gemini", model=model, effort="high")


def _recording(home, project, sid=SID):
    path = home / ".gemini" / "tmp" / project / "chats" / f"session-2026-10-23T00-00-{sid[:8]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessionId": sid, "projectHash": project}) + "\n")
    return path


def _fake_machine(tmp_path, monkeypatch, *, create=True, delay=.8):
    home = tmp_path / "home with spaces"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "work with spaces"
    cwd.mkdir()
    binary = tmp_path / "fake gemini"
    binary.write_text(f"#!{sys.executable}\n" + '''import json,os,sys,time
from pathlib import Path
home=Path(os.environ['HOME'])
args=sys.argv[1:]
(home/'argv.json').write_text(json.dumps({'argv':args,'tty':os.isatty(1),'cwd':os.getcwd()}))
sid=args[args.index('--session-id')+1] if '--session-id' in args else args[args.index('--resume')+1]
''' + ('''p=home/'.gemini/tmp/fake project/chats'/('session-2026-10-23T00-00-'+sid[:8]+'.jsonl')
p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(json.dumps({'sessionId':sid,'projectHash':'fake'})+'\\n')
''' if create else "") + f"time.sleep({delay})\n")
    binary.chmod(0o700)
    monkeypatch.setattr(launch.uuid, "uuid4", lambda: SID)
    return home, launch.local_machine("local", cwd=str(cwd), gemini_bin=str(binary))


def _plan(machine, **kwargs):
    return launch.build_launch(machine, provider="gemini", tmux_session="fake-gemini",
                               launch_model="gemini-3.1-pro-preview", launch_effort="default", **kwargs)


def _run(plan, home, *, timeout=6):
    master, slave = pty.openpty()
    proc = subprocess.Popen(["/bin/sh", "-c", plan.command], env={"HOME": str(home), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    try:
        rc = proc.wait(timeout=timeout)
        # The owned shell watcher must stop after the provider exits.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            pytest.fail("Gemini launch left an owned helper running after provider exit")
        return rc
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        os.close(master)


def test_fake_launch_preserves_tty_argv_and_links_native_recording(tmp_path, monkeypatch):
    home, machine = _fake_machine(tmp_path, monkeypatch)
    plan = _plan(machine)
    assert _run(plan, home) == 0
    receipt = json.loads((home / "argv.json").read_text())
    assert receipt == {"argv": ["--model", "gemini-3.1-pro-preview", "--session-id", SID], "tty": True, "cwd": machine.cwd}
    assert Path(plan.jsonl_path).is_symlink()
    assert json.loads(Path(plan.jsonl_path).read_text())["sessionId"] == SID
    assert transcript_allowed(plan.jsonl_path, "gemini")


def test_watcher_uses_full_id_and_handles_spaces(tmp_path, monkeypatch):
    home, machine = _fake_machine(tmp_path, monkeypatch, create=False)
    _recording(home, "a decoy", OTHER)
    valid = _recording(home, "z correct", SID)
    plan = _plan(machine)
    assert _run(plan, home) == 0
    assert Path(plan.jsonl_path).resolve() == valid


def test_watcher_refuses_ambiguous_recordings(tmp_path, monkeypatch):
    home, machine = _fake_machine(tmp_path, monkeypatch, create=False)
    _recording(home, "one")
    _recording(home, "two")
    plan = _plan(machine)
    assert _run(plan, home) == 0
    assert not Path(plan.jsonl_path).is_symlink()


def test_no_recording_provider_exit_cleans_watcher(tmp_path, monkeypatch):
    home, machine = _fake_machine(tmp_path, monkeypatch, create=False, delay=.1)
    plan = _plan(machine)
    assert _run(plan, home) == 0
    assert not Path(plan.jsonl_path).exists()


def test_absent_binary_does_not_start_watcher(tmp_path, monkeypatch):
    home, machine = _fake_machine(tmp_path, monkeypatch, create=False)
    from dataclasses import replace
    machine = replace(machine, gemini_bin=str(tmp_path / "absent-gemini"))
    assert _run(_plan(machine), home) != 0
    assert not (home / "argv.json").exists()


def test_resume_uses_exact_recorded_path_and_distinct_cwd(tmp_path, monkeypatch):
    home, machine = _fake_machine(tmp_path, monkeypatch, create=False)
    native = _recording(home, "resume project")
    resume_cwd = tmp_path / "resume cwd"
    resume_cwd.mkdir()
    plan = _plan(machine, resume_session_id=SID, resume_jsonl_path=str(native), resume_cwd=str(resume_cwd))
    assert plan.jsonl_path == str(native)
    assert _run(plan, home) == 0
    receipt = json.loads((home / "argv.json").read_text())
    assert receipt["cwd"] == str(resume_cwd)
    assert receipt["argv"][-2:] == ["--resume", SID]


def test_session_id_and_resume_validation(tmp_path, monkeypatch):
    home, machine = _fake_machine(tmp_path, monkeypatch, create=False)
    with pytest.raises(ValueError, match="gemini_session_id_invalid"):
        _plan(machine, resume_session_id="bad; touch should-never-exist", resume_jsonl_path=str(home / "record.jsonl"))
    with pytest.raises(ValueError, match="gemini_resume_recording_required"):
        _plan(machine, resume_session_id=SID)


def test_transcript_policy_rejects_outside_root_link(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{}\n')
    stable = tmp_path / "project/.gemini/sessions/session.jsonl"
    stable.parent.mkdir(parents=True)
    stable.symlink_to(outside)
    assert not transcript_allowed(str(stable), "gemini")
    native = _recording(tmp_path, "correct")
    stable.unlink()
    stable.symlink_to(native)
    assert transcript_allowed(str(stable), "gemini")
    assert not transcript_allowed(str(stable), "gemini", {"provider":"claude", "transcript_root":str(tmp_path)})


def test_native_snapshots_and_late_tool_patches_deduplicate():
    records = [
        {"id":"g1", "type":"gemini", "content":"Checking", "toolCalls":[{"id":"t1", "name":"read_file", "args":{"path":"a"}}]},
        {"id":"g1", "type":"gemini", "content":"Checking", "toolCalls":[{"id":"t1", "name":"read_file", "args":{"path":"a"}, "result":[{"functionResponse":{"response":{"output":"ok"}}}]}]},
        {"$patch":{"updates":[{"id":"g1", "toolCalls":[{"id":"t1", "result":[{"functionResponse":{"response":{"output":"ok"}}}]}]}]}},
    ]
    events = normalize_gemini_jsonl_records(records, host="local", session_name="test", default_session_id=SID)
    unique = {_identity_key(e): e for e in events}
    assert sorted(e["kind"] for e in unique.values()) == ["ASSIST_TEXT", "TOOL_RESULT", "TOOL_USE"]
    assert [e["text"] for e in unique.values() if e["kind"] == "TOOL_RESULT"] == ["ok"]
    patch_only = normalize_gemini_jsonl_records(records[2:], host="local", session_name="test", default_session_id=SID)
    assert len(patch_only) == 1 and patch_only[0]["kind"] == "TOOL_RESULT"


@pytest.mark.parametrize("record", [None, [], {"type":"gemini", "toolCalls":3},
    {"type":"gemini", "toolCalls":[{"result":[{"functionResponse":"bad"}]}]}, {"$patch":[]}, {"$rewindTo":"u1"}])
def test_malformed_or_unsupported_native_records_do_not_crash(record):
    normalize_gemini_jsonl_records([record], host="local", session_name="test", default_session_id=SID)


def test_wrapped_echo_and_unrelated_or_error_evidence_do_not_false_confirm():
    idle = (FIXTURES / "gemini_idle_pane.txt").read_text()
    prompt = "a long prompt with\nmultiple wrapped lines"
    wrapped = idle.replace(" >   Type", " > a long prompt with\n   multiple wrapped lines\n >   Type")
    assert submission_proven_after(wrapped, idle, prompt, "gemini")
    stale = idle.replace(" >   Type", " > target prompt\n >   Type")
    unrelated = stale.replace(" >   Type", " > unrelated prompt\n >   Type")
    assert not submission_proven_after(unrelated, stale, "target prompt", "gemini")
    draft = (FIXTURES / "gemini_draft_pane.txt").read_text() + "\nAPI Error: old issue\n"
    assert not submission_proven_after(draft, idle, "PREDICATE_TEST_ONE", "gemini")
    assert not gemini_prompt_ready("Authentication required\n> login\n")


def test_watcher_stops_at_poll_budget_while_provider_is_alive(tmp_path, monkeypatch):
    from dataclasses import replace
    home, machine = _fake_machine(tmp_path, monkeypatch, create=False, delay=2)
    tools = tmp_path / "tools"
    tools.mkdir()
    sleep = tools / "sleep"
    sleep.write_text('#!/bin/sh\nprintf x >> "$HOME/polls"\n')
    sleep.chmod(0o700)
    machine = replace(machine, agent_orch_bin_dir=str(tools))
    plan = _plan(machine)
    assert _run(plan, home) == 0
    assert len((home / "polls").read_text()) == 240
    assert not Path(plan.jsonl_path).exists()


def test_managed_account_mode_does_not_fallback_or_rebind_other_providers():
    from test_account_runtime import setup
    from provider_accounts import AccountError
    async def run():
        store, runtime = await setup()
        try:
            before = await runtime.run(lambda accounts: accounts.defaults())
            with pytest.raises(AccountError):
                await runtime.choose_launch(host="host-a", provider="gemini", model="gemini-3.1-pro-preview", effort="default", message={})
            assert await runtime.run(lambda accounts: accounts.defaults()) == before
            assert (await runtime.choose_launch(host="host-a", provider="codex", model="fixture", effort="high", message={})).provider == "codex"
        finally:
            store.stop()
    asyncio.run(run())


def test_bootstrap_parser_accepts_explicit_gemini_without_side_effects(tmp_path):
    import importlib.util
    path = Path(__file__).resolve().parents[3] / "tools/bootstrap_assistant.py"
    spec = importlib.util.spec_from_file_location("gemini_bootstrap_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.parser().parse_args([
        "--credential-file", str(tmp_path / "unused"), "--physical-host", "local", "--name", "fixture",
        "--provider", "gemini", "--model", "gemini-3.1-pro-preview", "--effort", "default",
        "--private-workspace", str(tmp_path), "--url", "ws://localhost:1",
        "--instructions-file", str(tmp_path / "instructions.md"),
    ])
    assert args.provider == "gemini"
    assert not (tmp_path / "unused").exists()
    args.credential_file.write_text("synthetic-credential")
    args.credential_file.chmod(0o600)
    args.instructions_file.write_text("Synthetic assistant instructions")
    args.dry_run = True
    result = module.bootstrap(args)
    assert result["requested_tuple"]["provider"] == "gemini"
    assert result["mutated"] is False


def test_daemon_tuple_resolution_carries_native_identity_and_configured_binary(tmp_path, monkeypatch):
    from spawnctl import SpawnCtl
    home, machine = _fake_machine(tmp_path, monkeypatch)
    ctl = SpawnCtl(store=None, sessions=None, tmux=object(), machine=machine, hosts=None)
    command, resolution, overrides = asyncio.run(ctl._resolve_launch(
        {"provider":"gemini", "model":"gemini-3.1-pro-preview", "effort":"default"}, "local", "v2-fixture"))
    assert resolution["actual_launch_tuple"] == {"provider":"gemini", "model":"gemini-3.1-pro-preview", "effort":"default"}
    assert overrides["claude_session_id"] == SID
    assert overrides["jsonl_path"].endswith(f"/.gemini/sessions/{SID}.jsonl")
    assert overrides["observer_binding"]["executable"] == machine.gemini_bin
    assert "--session-id" in command
