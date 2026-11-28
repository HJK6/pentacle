"""Advertised protocol history and the supported authenticated wire contract."""
import asyncio
import json
import re
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

import independent_client_example as example
from test_client_contract_probe import _contract_daemon
from test_work_lanes import run

SERVICE_DIR = Path(__file__).resolve().parents[2]
ROOT = SERVICE_DIR.parents[1]
TESTED_PROTOCOL_VERSIONS = [2]


def identity():
    return json.loads((SERVICE_DIR / "daemon_identity.json").read_text())


def test_protocol_version_history_matches_tested_range():
    metadata = identity()
    declared = set(range(metadata["min_protocol_version"], metadata["max_protocol_version"] + 1))
    tested = set(TESTED_PROTOCOL_VERSIONS)
    guide = (ROOT / "docs/chat_protocol.md").read_text()
    table = guide.split("## Protocol versions\n", 1)[1].split("\n## ", 1)[0]
    supported = {int(version) for version, status in re.findall(
        r"^\|\s*(\d+)\s*\|[^\n]*\|\s*(supported|dropped)\s*\|\s*$", table, re.M
    ) if status == "supported"}
    assert tested == declared, f"tested versions {sorted(tested)} disagree with identity range {sorted(declared)}"
    assert supported == declared, f"guide supported versions {sorted(supported)} disagree with identity range {sorted(declared)}"


@pytest.mark.parametrize("version", TESTED_PROTOCOL_VERSIONS)
def test_supported_version_authenticated_history(tmp_path, version):
    async def check(env):
        async with _contract_daemon(env, tmp_path) as fixture:
            _, url, stream, _, _, _, _, _, key_file, seed, _, _ = fixture
            credential = example.load_credential(key_file)
            async with asyncio.timeout(15):
                async with connect(url, origin=example.FIXTURE_ORIGIN, proxy=None, open_timeout=3) as wire:
                    client = example.FixtureSocket(wire, stream)
                    welcome = await client.receive()
                    assert welcome["type"] == "welcome"
                    for field, expected in identity().items():
                        assert welcome[field] == expected, f"welcome {field} differs from identity"
                    hello = example.authenticated_hello(welcome, credential, "version-hello")
                    hello["protocol_version"] = version
                    await client.send(hello)
                    reply = await client.receive()
                    assert reply["type"] == "hello" and reply["request_id"] == "version-hello", f"version {version} hello rejected: {reply}"
                    snapshot = await client.receive()
                    assert snapshot["type"] == "snapshot"
                    assert any(row["stream_id"] == stream for row in snapshot["sessions"])
                    events = await client.history()
                    assert set(events) == set(seed[-example.PAGE_SIZE:]), f"version {version} seeded history missing"
    run(check, str(tmp_path / "sessions.db"))


@pytest.mark.parametrize("boundary", ["below", "above"])
def test_outside_supported_range_is_rejected(tmp_path, boundary):
    async def check(env):
        async with _contract_daemon(env, tmp_path) as fixture:
            _, url, _, _, _, _, _, _, key_file, _, _, _ = fixture
            metadata = identity()
            version = (metadata["min_protocol_version"] - 1 if boundary == "below"
                       else metadata["max_protocol_version"] + 1)
            async with asyncio.timeout(15):
                async with connect(url, origin=example.FIXTURE_ORIGIN, proxy=None, open_timeout=3) as wire:
                    welcome = json.loads(await wire.recv())
                    hello = example.authenticated_hello(welcome, example.load_credential(key_file), "out-of-range")
                    hello["protocol_version"] = version
                    await wire.send(json.dumps(hello))
                    reply = json.loads(await wire.recv())
                    assert reply == {"type": "hello.error", "request_id": "out-of-range",
                                     "error_code": "unsupported_protocol",
                                     "supported_protocol_min": metadata["min_protocol_version"],
                                     "supported_protocol_max": metadata["max_protocol_version"]}
    run(check, str(tmp_path / "sessions.db"))
