"""Closed Codex lifecycle continuity at the retained account and native CLI boundaries."""
import asyncio
from dataclasses import replace
import json
import subprocess
import sys

import pytest

import launch
from sessions import VerbError
from spawnctl import SpawnCtl
from test_account_rotation import binding
from test_account_runtime import AUTH, setup
from test_spawn_resume import RESUME_ID, ResumeTmux, _install_successful_spawn

HOST = "host-a"
NAME = "review-child"
PARENT = "host-a:front-desk"


async def retained(store, account, path, *, name=NAME, closed=True):
    row = await store.open_session(HOST, name, provider="codex", parent_stream_id=PARENT,
                                   role="qa", account_binding=account.public(), pane_pid="41",
                                   observer_binding={"executable": account.executable})
    stat = path.stat()
    assert await store.bind_observer_transcript(
        row["stream_id"], generation=row["session_generation"], pane_pid="41",
        expected=row["observer_binding"], transcript={"path": str(path), "provider": "codex",
            "session_id": RESUME_ID, "file_id": [stat.st_dev, stat.st_ino]})
    if closed:
        await store.mark_closed(HOST, name, closed_at="2026-01-12T00:00:00Z",
                                pane_status="pane_dead", expected_generation=row["session_generation"])
    return await store.fetch_session(HOST, name)


def fixture_paths(tmp_path):
    home = tmp_path / "account home"
    path = home / "sessions" / "2026/01/12" / f"rollout-2026-01-12T00-00-00-{RESUME_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": RESUME_ID, "cwd": str(tmp_path)}}) + "\n")
    executable = tmp_path / "capture-argv"
    executable.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                          "print(json.dumps([sys.argv[1:], os.getcwd(), os.environ['CODEX_HOME']]))\n")
    executable.chmod(0o700)
    return replace(binding("primary"), home=str(home), generation="test-resume",
                   executable=str(executable)), path


@pytest.fixture
def codex_account_on_path(tmp_path, monkeypatch):
    account, path = fixture_paths(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").symlink_to(account.executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    return account, path


def test_codex_resume_refuses_missing_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    machine = launch.local_machine(HOST, cwd=str(tmp_path))
    with pytest.raises(ValueError, match="local provider executable 'codex' is not discoverable"):
        launch.build_launch(machine, provider="codex", tmux_session=NAME,
                            launch_model="gpt-6-luna", launch_effort="low", resume_session_id=RESUME_ID)


def test_codex_resume_refuses_changed_identity_after_lookup(tmp_path, monkeypatch):
    async def run():
        store, runtime = await setup(str(tmp_path / "retained seats.db"))
        try:
            account, path = fixture_paths(tmp_path)
            prior = await retained(store, account, path)

            async def changed(host, name):
                assert (host, name) == (HOST, NAME)
                binding = prior["observer_binding"]
                transcript = {**binding["transcript"], "session_id": "00000000-0000-4000-8000-000000000002"}
                return {**prior, "observer_binding": {**binding, "transcript": transcript}}

            monkeypatch.setattr(store, "fetch_session", changed)
            tmux = ResumeTmux()
            ctl = SpawnCtl(store, runtime.sessions, tmux=tmux)
            with pytest.raises(VerbError) as exc:
                await ctl._codex_resume_target({}, HOST, tmux, RESUME_ID)
            assert exc.value.code == "resume_foreign_target"
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("explicit", [False, True])
def test_closed_codex_resume_reopens_same_row_and_launches_native_session(tmp_path, explicit):
    async def run():
        store, runtime = await setup()
        try:
            account, path = fixture_paths(tmp_path)
            await runtime.run(lambda registry: registry.register(account))
            prior = await retained(store, account, path)
            tmux = ResumeTmux()
            machine = launch.local_machine(HOST, cwd=str(tmp_path), codex_bin=account.executable)
            ctl = SpawnCtl(store, runtime.sessions, tmux=tmux, machine=machine)
            ctl.account_runtime = runtime
            commands = _install_successful_spawn(ctl, runtime.sessions, tmux)
            msg = {"provider": "codex", "resume_session_id": RESUME_ID, "model": "gpt-6-luna",
                   "effort": "low", "_auth_context": AUTH, "request_id": "resume-test"}
            if explicit:
                msg["account"] = account.alias
            await ctl.spawn(msg, HOST)
            row = await store.fetch_session(HOST, NAME)
            assert row["status"] == "open"
            assert row["stream_id"] == prior["stream_id"]
            assert row["session_generation"] != prior["session_generation"]
            assert row["account_binding"] == account.public()
            assert row["role"] == "qa" and row["parent_stream_id"] == PARENT
            assert not row["claude_session_id"] and not row["jsonl_path"]
            command, = commands
            output = subprocess.check_output(["/bin/sh", "-c", command], text=True)
            argv, cwd, home = json.loads(output)
            assert argv[-2:] == ["resume", RESUME_ID]
            assert home == account.home and cwd == str(tmp_path)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("prompt", ["", "Use the earlier review."])
def test_codex_resume_persists_generation_before_pane_creation(tmp_path, prompt, codex_account_on_path):
    async def run():
        store, runtime = await setup()
        try:
            account, path = codex_account_on_path
            await runtime.run(lambda registry: registry.register(account))
            prior = await retained(store, account, path)
            tmux = ResumeTmux()
            ctl = SpawnCtl(store, runtime.sessions, tmux=tmux,
                           machine=launch.local_machine(HOST, cwd=str(tmp_path)))
            ctl.account_runtime = runtime
            _install_successful_spawn(ctl, runtime.sessions, tmux)
            create_pane = ctl._spawn_fenced
            persisted = []

            async def inspect_intent(*args, **kwargs):
                reservation, = await store.reservations()
                intent = json.loads(reservation["payload"])
                generation = intent["open_fields"]["session_generation"]
                assert intent["delivery_receipt"]["resume_generation"] == generation
                assert generation != prior["session_generation"]
                persisted.append(generation)
                return await create_pane(*args, **kwargs)

            ctl._spawn_fenced = inspect_intent
            msg = dict(provider="codex", model="gpt-6-luna", effort="low", resume_session_id=RESUME_ID,
                       request_id="resume-intent", initial_prompt=prompt, _auth_context=AUTH)
            reply = await ctl._spawn_guarded(msg, HOST)
            assert persisted == [reply["session"]["session_generation"]]
            # Unrelated work in the same task must receive an ordinary prompt receipt.
            _, receipt = await ctl._prepare_brief("", tmux, stage_host=HOST, delivery_receipt={})
            assert receipt == {"state": "not_requested"}
        finally:
            store.stop()
    asyncio.run(run())


def test_codex_resume_preserves_staged_prompt_as_one_argument(tmp_path):
    account, path = fixture_paths(tmp_path)
    prompt = tmp_path / "initial prompt"
    prompt.write_text("-question with spaces\n")
    plan = launch.build_launch(launch.local_machine(HOST, cwd=str(tmp_path)), provider="codex",
                               tmux_session=NAME, launch_model="gpt-6-luna", launch_effort="low",
                               initial_prompt_file=str(prompt), resume_session_id=RESUME_ID,
                               resume_jsonl_path=str(path), resume_cwd=str(tmp_path), account_binding=account)
    argv, _, _ = json.loads(subprocess.check_output(["/bin/sh", "-c", plan.command], text=True))
    assert argv[-4:] == ["resume", RESUME_ID, "--", prompt.read_text()]


@pytest.mark.parametrize(("case", "code"), [
    ("open", "resume_session_already_live"), ("pane", "resume_session_already_live"),
    ("unknown", "resume_target_probe_failed"), ("mismatch", "account_resume_cross_home_forbidden"),
    ("missing", "resume_transcript_not_found"), ("foreign", "resume_foreign_target"),
    ("duplicate_row", "resume_foreign_target"), ("replaced", "resume_foreign_target"),
    ("cross_home", "account_resume_cross_home_forbidden"), ("duplicate_file", "resume_foreign_target"),
])
def test_codex_resume_refuses_before_mutation(tmp_path, case, code):
    async def run():
        account, path = fixture_paths(tmp_path)
        store, runtime = await setup()
        try:
            if case == "cross_home":
                path = path.rename(tmp_path / path.name)
            row = await retained(store, account, path, closed=case != "open")
            if case == "duplicate_row":
                await retained(store, account, path, name="second-reviewer")
            if case == "duplicate_file":
                path.with_name("rollout-2000-01-01T00-00-00-" + RESUME_ID + ".jsonl").write_bytes(path.read_bytes())
            if case == "replaced":
                path.rename(tmp_path / "original")
                path.write_bytes((tmp_path / "original").read_bytes())
            if case == "missing":
                path.unlink()
            if case == "foreign":
                path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "other", "cwd": str(tmp_path)}}))
            tmux = ResumeTmux()
            if case == "pane":
                tmux.alive.add(NAME)
            if case == "unknown":
                async def unreachable(name):
                    return "unreachable"
                tmux.session_state = unreachable
            ctl = SpawnCtl(store, runtime.sessions, tmux=tmux,
                           machine=launch.local_machine(HOST, cwd=str(tmp_path)))
            ctl.account_runtime = runtime
            msg = {"provider": "codex", "resume_session_id": RESUME_ID, "model": "gpt-6-luna",
                   "effort": "low", "_auth_context": AUTH, "request_id": "refusal"}
            if case == "mismatch":
                msg["account"] = "spare"
            with pytest.raises(VerbError) as exc:
                await ctl.spawn(msg, HOST)
            assert exc.value.code == code
            assert await store.fetch_session(HOST, NAME) == row
            assert not tmux.staged and not await store.reservations()
        finally:
            store.stop()
    asyncio.run(run())


def test_codex_resume_replays_only_its_own_delivered_generation(tmp_path, codex_account_on_path):
    async def run():
        store, runtime = await setup()
        try:
            account, path = codex_account_on_path
            await runtime.run(lambda registry: registry.register(account))
            await retained(store, account, path)
            tmux = ResumeTmux()
            ctl = SpawnCtl(store, runtime.sessions, tmux=tmux,
                           machine=launch.local_machine(HOST, cwd=str(tmp_path)))
            ctl.account_runtime = runtime
            commands = _install_successful_spawn(ctl, runtime.sessions, tmux)
            msg = dict(provider="codex", resume_session_id=RESUME_ID, account=account.alias,
                       model="gpt-6-luna", effort="low", _auth_context=AUTH,
                       objective="Resume review", request_id="original", idempotency_key="resume-once")
            first = await ctl.spawn(msg, HOST)
            row = first["session"]
            stat = path.stat()
            assert await store.bind_observer_transcript(
                row["stream_id"], generation=row["session_generation"], pane_pid="4321",
                expected=row["observer_binding"], transcript={"path": str(path), "provider": "codex",
                    "session_id": RESUME_ID, "file_id": [stat.st_dev, stat.st_ino]})
            # The pane fixture replaces the boot step that persists the delivered receipt.
            await store.set_spawn_outcome(HOST, NAME, "delivered", request_id=msg["request_id"],
                                          idempotency_key=msg["idempotency_key"],
                                          request_payload_hash=ctl._spawn_payload_hash(msg),
                                          delivery_receipt=first["initial_prompt_delivery"])
            replay = await ctl.spawn(dict(msg, request_id="retry"), HOST)
            assert replay["replayed"] and replay["stream_id"] == row["stream_id"]
            assert replay["session"]["session_generation"] == row["session_generation"]
            with pytest.raises(VerbError, match="live|closed"):
                await ctl.spawn(dict(msg, idempotency_key="different"), HOST)
            with pytest.raises(VerbError) as conflict:
                await ctl.spawn(dict(msg, account="spare"), HOST)
            assert conflict.value.code in {"idempotency_key_conflict", "account_resume_cross_home_forbidden"}
            await runtime.sessions.open(HOST, NAME, provider="codex", session_generation="successor")
            with pytest.raises(VerbError) as stale:
                await ctl.spawn(dict(msg, request_id="stale-retry"), HOST)
            assert stale.value.code == "replayed_stream_dead"
            assert len(commands) == 1
        finally:
            store.stop()
    asyncio.run(run())
