#!/usr/bin/env python3
"""Two-phase, payload-free smoke example for an owned disposable daemon."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

# Use the same proof implementation as credential issuance; no UI or SDK.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _shared import operator_auth  # noqa: E402

FIXTURE_ORIGIN = "https://example.invalid"
FIXTURE_INPUT = "Synthetic client contract turn."
FIXTURE_STREAM = re.compile(r"fixture:v2-client-contract-[0-9a-f]{32}")
PAGE_SIZE = 2
TIMEOUT = 15


class ContractError(Exception):
    """Fixed diagnostic text only; never put a reply or exception here."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def load_credential(path):
    # No terminal/history arguments containing secrets; reject symlinks and
    # files readable by other users. Open nonblocking to refuse a FIFO safely.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "Expected a private 0600 credential file.")
        return operator_auth.decode_envelope(handle.read(4096).strip())


def authenticated_hello(welcome, credential, request_id):
    challenge = welcome.get("auth", {}).get("operator", {})
    require(welcome.get("type") == "welcome"
            and challenge.get("protocol_version") == operator_auth.AUTH_PROTOCOL_VERSION
            and challenge.get("scheme") == operator_auth.AUTH_SCHEME, "Expected operator v2 welcome.")
    kind = credential["client_kind"]
    cid = credential["credential_id"]
    return {
        "type": "hello", "request_id": request_id, "client": kind,
        "capabilities": {"work_lanes_v1": True},
        "subscribe": {"include_subagents": False, "events_mode": "summary"},
        "auth_v2": {"scheme": operator_auth.AUTH_SCHEME, "credential_id": cid,
                    "proof": operator_auth.make_proof(credential["proof_key"], challenge["nonce"], cid, kind)},
    }


def sequence(event, stream):
    require(isinstance(event, dict) and event.get("stream_id") == stream
            and type(event.get("daemon_seq")) is int and event["daemon_seq"] > 0,
            "Expected durable stream and sequence on each transcript row.")
    return event["daemon_seq"]


class FixtureSocket:
    def __init__(self, wire, stream):
        self.wire, self.stream = wire, stream
        self.events = {}

    async def send(self, frame):
        await self.wire.send(json.dumps(frame))

    async def receive(self):
        frame = json.loads(await self.wire.recv())
        require(isinstance(frame, dict), "Expected a JSON object frame.")
        event = frame.get("event")
        if (frame.get("type") == "chat.event" and isinstance(event, dict)
                and event.get("stream_id") == self.stream and event.get("daemon_seq") is not None):
            self.events[sequence(event, self.stream)] = event
        return frame

    async def correlated(self, request_id):
        while True:
            frame = await self.receive()
            if frame.get("request_id") == request_id:
                require(not str(frame.get("type", "")).endswith(".error"),
                        "Daemon rejected a correlated request.")
                return frame

    async def rpc(self, verb, result, **fields):
        rid = uuid4().hex
        await self.send({"type": verb, "request_id": rid, **fields})
        frame = await self.correlated(rid)
        require(frame.get("type") == result, "Unexpected correlated reply type.")
        return frame

    async def history(self, *, before=None):
        rid = uuid4().hex
        await self.send({
            "type": "request_stream_events", "request_id": rid, "stream_id": self.stream,
            "limit": PAGE_SIZE, "chunk_limit": 1,
            **({"before_daemon_seq": before} if before is not None else {}),
        })
        events = {}
        while True:
            frame = await self.correlated(rid)
            require(frame.get("type") in ("request_stream_events.ok", "request_stream_events.chunk")
                    and frame.get("stream_id") == self.stream
                    and isinstance(frame.get("events"), list), "Unexpected history envelope.")
            for event in frame["events"]:
                seq = sequence(event, self.stream)
                require(seq not in events and (before is None or seq < before),
                        "History repeated an identity or crossed its exclusive cursor.")
                events[seq] = event
            require(len(events) <= PAGE_SIZE, "History exceeded its requested limit.")
            if frame["type"] == "request_stream_events.ok":
                require(frame.get("complete") is True, "History ended without completeness.")
                return events


@asynccontextmanager
async def open_client(url, credential, *, stream):
    async with asyncio.timeout(TIMEOUT):
        # With Origin the daemon does not grant its native loopback exception.
        # This synthetic Origin tests proof; it isn't browser support.
        async with connect(url, origin=FIXTURE_ORIGIN, proxy=None, open_timeout=3) as wire:
            client = FixtureSocket(wire, stream)
            rid = uuid4().hex
            await client.send(authenticated_hello(await client.receive(), credential, rid))
            hello = await client.correlated(rid)
            require(hello.get("type") == "hello", "Expected authenticated hello.")
            while True:
                frame = await client.receive()
                if frame.get("type") == "snapshot":
                    client.snapshot = frame
                    break
            yield client


def verify_target(snapshot, stream, generation):
    rows = snapshot.get("sessions")
    require(isinstance(rows, list), "Expected visible session inventory.")
    require(any(row.get("stream_id") == stream and row.get("session_generation") == generation
                for row in rows), "Selected fixture stream and generation are not visible.")
    return len(rows)


async def recover_history(client, anchor):
    """Subscribe first, then walk backward until the saved identity is seen."""
    before = None
    recovered = {}
    for _ in range(100):
        page = await client.history(before=before)
        if not page:
            break
        recovered.update(page)
        if anchor in page:
            return {**recovered, **client.events}
        before = min(page)
        if before < anchor:
            break
    raise ContractError("Checkpoint anchor no longer available; cannot prove gap-free recovery.")


def write_checkpoint(path, state):
    # Same-directory rename gives an all-or-nothing update to the owned file.
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            json.dump(state, handle)
            handle.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


async def run(args, credential):
    endpoint = urlsplit(args.url)
    require(endpoint.scheme == "ws" and endpoint.hostname in {"127.0.0.1", "::1"}
            and endpoint.port and endpoint.username is None and endpoint.password is None
            and endpoint.path in {"", "/"} and not endpoint.query and not endpoint.fragment,
            "Example requires a literal loopback fixture websocket.")
    require(FIXTURE_STREAM.fullmatch(args.stream) and args.generation,
            "Select an explicitly provisioned disposable fixture stream and generation.")

    state = {"stream_id": args.stream, "generation": args.generation}
    if args.phase == "start":
        # Reserve before sending. On failure don't reuse this checkpoint or
        # auto-resend: send acknowledgement and durable echo are separate.
        fd = os.open(args.checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    else:
        with args.checkpoint.open(encoding="utf-8") as handle:
            saved = json.load(handle)
        require(all(saved.get(key) == value for key, value in state.items()),
                "Checkpoint belongs to a different fixture.")
        saved_seqs = saved.get("sequences")
        require(isinstance(saved_seqs, list) and saved_seqs
                and all(type(seq) is int and seq > 0 for seq in saved_seqs)
                and saved_seqs == sorted(set(saved_seqs))
                and saved.get("anchor") == saved_seqs[-1], "Invalid checkpoint identities.")

    async with open_client(args.url, credential, stream=args.stream) as client:
        session_count = verify_target(client.snapshot, args.stream, args.generation)
        if args.phase == "start":
            listed = await client.rpc("list_sessions", "list_sessions.ok", include_closed=False)
            active_count = verify_target({"sessions": listed.get("active")}, args.stream, args.generation)
            require(client.snapshot.get("capabilities", {}).get("work_lanes_v1") is True
                    and isinstance(client.snapshot.get("work_lanes", {}).get("lanes"), list),
                    "Fixture must advertise and project work_lanes_v1.")
            lanes = (await client.rpc("work_lanes.list", "work_lanes.list.ok", limit=20))["lanes"]
            require(isinstance(lanes, list), "Expected a lane list.")
            newer = await client.history()
            require(len(newer) == PAGE_SIZE, "Fixture must have a full newest history page.")
            older = await client.history(before=min(newer))
            require(len(older) == PAGE_SIZE, "Fixture must have a full older history page.")
            result = await client.rpc("send", "send.result", to_stream_id=args.stream,
                                      optimistic_id=uuid4().hex, text=FIXTURE_INPUT)
            require(result.get("to_stream_id") == args.stream
                    and result.get("submission_confirmed") is True, "Fixture send was not confirmed.")
            echoes = []
            while not echoes:
                echoes = [seq for seq, row in client.events.items()
                          if seq > max(newer) and row.get("kind") == "USER"
                          and row.get("text") == FIXTURE_INPUT]
                if not echoes:
                    await client.receive()
            require(len(echoes) == 1, "Fixture produced multiple send echoes.")
            sequences = sorted({*older, *newer, *client.events})
            counts = {"snapshot_sessions": session_count, "active_sessions": active_count,
                      "lanes": len(lanes), "newer_rows": len(newer), "older_rows": len(older),
                      "send_results": 1, "live_echoes": len(echoes)}
        else:
            recovered = await recover_history(client, saved["anchor"])
            sequences = sorted({*saved_seqs, *(
                seq for seq in recovered if seq > saved["anchor"]
            )})
            counts = {"resumed_sessions": session_count, "new_rows": len(sequences) - len(saved_seqs)}

        write_checkpoint(args.checkpoint, {**state, "anchor": sequences[-1], "sequences": sequences})
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("start", "resume"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        counts = asyncio.run(run(args, load_credential(args.credential_file)))
    except ContractError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (OSError, TimeoutError, KeyError, TypeError, ValueError, AttributeError,
            WebSocketException, operator_auth.OperatorAuthError):
        print("Fixture transport, credential, or checkpoint failed.", file=sys.stderr)
        return 1
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
