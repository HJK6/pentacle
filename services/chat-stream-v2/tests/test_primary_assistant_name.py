"""The primary assistant's name and helpers come from config, not code.

Public defaults are neutral; a deployment that sets its own values keeps its
stored keys (binding rows, legacy mirror key, alias, retro identity). Each case
runs in a fresh interpreter because these values are read at import.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATHS = [ROOT / "services/chat-stream-v2", ROOT / "services", ROOT / "services/chat-stream-v2/tools",
         ROOT / "services/agent-orch", ROOT / "mic-server", ROOT]
CONFIGURED = {"PENTACLE_ASSISTANT_PRIMARY_NAME": "zed", "PENTACLE_COSMO_ASSISTANT_AUTHOR": "zed",
              "PENTACLE_SPEAK_HELPER": "zed-say",
              "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "desk:assistant"}


def run(code: str, env: dict[str, str]) -> str:
    base = {k: v for k, v in os.environ.items() if k not in CONFIGURED and not k.startswith("PENTACLE_SATELLITE_")}
    base["PYTHONPATH"] = os.pathsep.join(map(str, PATHS))
    done = subprocess.run([sys.executable, "-c", code], env={**base, **env}, cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip()


@pytest.mark.parametrize("env,name", [({}, "assistant"), (CONFIGURED, "zed")])
def test_composite_map_alias_and_digest_follow_primary_name(env, name):
    out = run(
        "import server, assistant_composite as a, front_desk_digest as f\n"
        "from agent_orch import cli\n"
        "cfgs = a.AssistantCompositeConfig.all_from_env()\n"
        "print(sorted(cfgs), a.AssistantCompositeConfig().name, server.ASSISTANT_ALIAS, cli.ASSISTANT_ALIAS)\n"
        "from dataclasses import replace\n"
        "c = replace(a.AssistantCompositeConfig(), enabled=True, direct_primary_stream_id='s:1', direct_primary_generation='g')\n"
        "print(f.FrontDeskDigest(None, lambda: c).binding(),"
        " f.FrontDeskDigest(None, lambda: replace(c, name='daff')).binding())\n",
        env)
    first, second = out.splitlines()
    assert first == f"{sorted([name, 'daff'])} {name} {name} {name}"
    assert second == "('s:1', 'g') None"


@pytest.mark.parametrize("env,name", [({}, "assistant"), (CONFIGURED, "zed")])
def test_binding_seed_readers_and_legacy_mirror_key_follow_primary_name(env, name):
    out = run(
        "import sqlite3, store, store_assistant_binding as b\n"
        "conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row\n"
        "conn.execute('CREATE TABLE v2_assistant_direct_binding (id INTEGER PRIMARY KEY CHECK(id=1),"
        "stream_id TEXT,generation TEXT,revision INTEGER NOT NULL,updated_at TEXT NOT NULL)')\n"
        "conn.execute(\"INSERT INTO v2_assistant_direct_binding VALUES (1,'s:1','g',3,'t')\")\n"
        "b.migrate_binding_to_named(conn)\n"
        "print([tuple(r) for r in conn.execute('SELECT name,revision FROM v2_assistant_direct_binding')])\n"
        "print(b._binding_conn(conn, {}, include_target=False)['stream_id'])\n"
        "b.rollback_binding_to_single(conn)\n"
        "print([tuple(r) for r in conn.execute('SELECT id,stream_id FROM v2_assistant_direct_binding')])\n"
        f"print(store.Store._mirror_kv_key({name!r}), store.Store._mirror_kv_key('daff'))\n",
        env)
    assert out.splitlines() == [f"[('{name}', 3)]", "s:1", "[(1, 's:1')]",
                                "assistant.mirror.enabled assistant.mirror.daff.enabled"]


@pytest.mark.parametrize("env,expected", [
    ({}, "assistant assistant-say assistant ws://127.0.0.1:7791"),
    (CONFIGURED,
     "zed zed-say zed ws://127.0.0.1:7791"),
    ({"PENTACLE_SATELLITE_WS": "ws://127.0.0.1:2"}, "assistant assistant-say assistant ws://127.0.0.1:2"),
])
def test_household_speak_retro_and_satellite_config(env, expected):
    out = run(
        "import household, satellite, daily_retro\n"
        "from speaker_service import SpeakerService\n"
        "helper = SpeakerService().contract('c1')['helper'].split()[0]\n"
        "print(household.COSMO_ASSISTANT, helper, daily_retro.primary_name(),"
        " satellite.SatelliteConfig.from_env().coordinator_ws)\n",
        env)
    assert out == expected


@pytest.mark.parametrize("env,ok", [({}, False), (CONFIGURED, True)])
def test_work_lane_composite_id_comes_from_env(env, ok):
    out = run(
        "from agent_orch import work_lane_cli as w\n"
        "print(w.DEFAULT_COMPOSITE or '<required>')\n",
        env)
    assert out == ("desk:assistant" if ok else "<required>")
