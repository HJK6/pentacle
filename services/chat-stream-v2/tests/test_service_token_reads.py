"""Service credentials are sampled once per auth context and rechecked on the next."""

import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path

import pytest

import server as auth
from store import Store
from test_report_producer_principal import config as report_config
from test_system_notification_producer import Peer


SYSTEM = auth.FIXED_SYSTEM_PRODUCER_STREAM_ID
BRIEF = "examplehost:wmi-daily-brief"
REPORT = "examplehost:daily-report"
WATCHER = "examplehost:account-watcher"
FILE_ENVS = {
    SYSTEM: auth.SYSTEM_PRODUCER_STREAM_TOKEN_FILE_ENV,
    auth.screen_monitor_principal.ACTOR: auth.screen_monitor_principal.TOKEN_FILE_ENV,
    auth.screen_acquisitions_principal.ACTOR: auth.screen_acquisitions_principal.TOKEN_FILE_ENV,
    auth.WORK_REPORT_READER: auth.WORK_REPORT_READER_TOKEN_FILE_ENV,
    auth.WMI_BACKUP_PRODUCER_STREAM_ID: auth.WMI_BACKUP_STREAM_TOKEN_FILE_ENV,
    auth.BOT_MESSAGING_PRODUCER_STREAM_ID: auth.BOT_MESSAGING_STREAM_TOKEN_FILE_ENV,
}
ACTORS = (*FILE_ENVS, BRIEF, REPORT, WATCHER)


def token(actor):
    return f"synthetic-unique-token-for-{actor}"


def write_private(path, content):
    path.write_text(content)
    path.chmod(0o600)
    return path


@pytest.fixture
def credentials(monkeypatch, tmp_path):
    root = tmp_path.resolve()
    paths = {actor: write_private(root / f"token-{i}", token(actor))
             for i, actor in enumerate(ACTORS)}
    for actor, env in FILE_ENVS.items():
        monkeypatch.setenv(env, str(paths[actor]))
    monkeypatch.setenv(auth.SYSTEM_PRODUCER_STREAM_ID_ENV, SYSTEM)
    configs = {
        BRIEF: (auth.wmi_brief_producer.CONFIG_ENV, {
            "stream_id": BRIEF, "token_file": str(paths[BRIEF]),
            "composite_stream_id": "examplehost:desk", "ops_stream_id": "examplehost:ops",
        }),
        REPORT: (auth.report_producer.CONFIG_ENV, report_config(token_file=str(paths[REPORT]))),
        WATCHER: ("PENTACLE_PROVIDER_ACCOUNT_POLICY", {"watchers": {
            WATCHER: {"host": "examplehost", "token_file": str(paths[WATCHER])},
        }}),
    }
    config_paths = {}
    for i, (actor, (env, payload)) in enumerate(configs.items()):
        config_paths[actor] = write_private(root / f"config-{i}.json", json.dumps(payload))
        monkeypatch.setenv(env, str(config_paths[actor]))
    return paths, config_paths


def hello(actor, supplied):
    return {"type": "hello", "from_stream_id": actor, "stream_token": supplied,
            "subscribe": {"snapshot": False, "mode": "rpc"}}


def test_seat_auth_opens_each_service_file_once(credentials, monkeypatch, tmp_path):
    paths, configs = credentials
    tracked = {*paths.values(), *configs.values()}
    opens = Counter()
    open_path = Path.open

    def counted_open(path, *args, **kwargs):
        if path in tracked:
            opens[path] += 1
        return open_path(path, *args, **kwargs)

    async def run():
        store = Store(str(tmp_path / "seat.db"))
        store.start()
        try:
            await store.open_session("examplehost", "seat", session_generation="seat-one")
            seat_token = "synthetic-ordinary-seat-credential"
            await store.grant_stream_token("examplehost", "seat", hashlib.sha256(seat_token.encode()).hexdigest(),
                                           auth.STREAM_TOKEN_HASH_VERSION)
            daemon = auth.Server(store=store)
            monkeypatch.setattr(Path, "open", counted_open)
            result = await daemon._auth_context(Peer(), {"type": "ping", "stream_token": seat_token,
                                                         "from_stream_id": "examplehost:seat"})
            assert result["token_verified"] and not result["service_attempted"]
        finally:
            store.stop()

    asyncio.run(run())
    assert {path.name: opens[path] for path in sorted(tracked)} == {path.name: 1 for path in sorted(tracked)}


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("case", ["own", "other", "shared", "missing", "nonprivate", "unconfigured"])
def test_service_auth_parity(credentials, monkeypatch, actor, case):
    paths, configs = credentials
    supplied = token(actor)
    other = BRIEF if actor == SYSTEM else SYSTEM
    if case == "other":
        supplied = token(other)
    elif case == "shared":
        paths[other].write_text(supplied)
    elif case == "missing":
        paths[actor].unlink()
    elif case == "nonprivate":
        paths[actor].chmod(0o644)
    elif case == "unconfigured":
        if actor == SYSTEM:
            monkeypatch.delenv(auth.SYSTEM_PRODUCER_STREAM_ID_ENV)
        elif actor in configs:
            configs[actor].unlink()
        else:
            monkeypatch.delenv(FILE_ENVS[actor])

    result = asyncio.run(auth.Server()._auth_context(Peer(), hello(actor, supplied)))
    # Established before the change. Only the fixed producer accepts a loose
    # file or keeps its token when a file principal is configured with it too.
    accepted = case == "own" or (actor == SYSTEM and case in {"nonprivate", "shared"})
    attempted = case != "unconfigured" or actor not in {REPORT, WATCHER}
    assert (result["service_attempted"], result["service_authenticated"], result["service_actor"]) == (
        attempted, accepted, actor if accepted else "",
    )


def test_brief_actor_cannot_overlap_report_config(credentials):
    paths, configs = credentials
    configs[REPORT].write_text(json.dumps(report_config(stream_id=BRIEF, token_file=str(paths[REPORT]))))
    result = asyncio.run(auth.Server()._auth_context(Peer(), hello(BRIEF, token(BRIEF))))
    assert result["service_attempted"] and not result["service_authenticated"]


@pytest.mark.parametrize("change", ["rotate", "remove"])
@pytest.mark.parametrize("supply_token", [False, True])
def test_bound_service_socket_rechecks_token_on_next_message(credentials, change, supply_token):
    paths, _ = credentials

    async def run():
        daemon, peer = auth.Server(), Peer()
        result = await daemon._auth_context(peer, hello(BRIEF, token(BRIEF)))
        assert result["service_authenticated"]
        msg = {"type": "tell", "from_stream_id": BRIEF}
        if supply_token:
            msg["stream_token"] = token(BRIEF)
        assert (await daemon._auth_context(peer, msg))["service_authenticated"]
        if change == "rotate":
            paths[BRIEF].write_text("synthetic-rotated-token")
        else:
            paths[BRIEF].unlink()
        result = await daemon._auth_context(peer, msg)
        assert result["service_attempted"] and not result["service_authenticated"]

    asyncio.run(run())
