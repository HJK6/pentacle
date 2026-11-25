"""Public socket and fixture provider readbacks for the chat contracts."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from websockets.sync.client import connect

from tests.soak.restart_matrix import LOCAL_HOST, spawn_payload

REPLY_S = 5
SPAWN_S = 6


def unique(label):
    return f"{label}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Frame:
    body: dict
    byte_count: int


class Client:
    def __init__(self, url, **kwargs):
        self.socket = connect(url, open_timeout=REPLY_S, close_timeout=0.5,
                              max_size=None, proxy=None, **kwargs)
        self.frames: list[Frame] = []

    def receive(self, timeout=REPLY_S):
        raw = self.socket.recv(timeout=timeout, decode=False)
        frame = Frame(json.loads(raw), len(raw))
        self.frames.append(frame)
        return frame

    def until(self, predicate, *, start, description, timeout=REPLY_S):
        for frame in self.frames[start:]:
            if predicate(frame.body):
                return frame
        deadline = time.monotonic() + timeout
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                frame = self.receive(timeout=remaining)
                if predicate(frame.body):
                    return frame
        except TimeoutError:
            pass
        last = [(frame.body.get("type"), frame.body.get("request_id"))
                for frame in self.frames[start:][-8:]]
        raise TimeoutError(f"{description} within {timeout}s; last frames {last}")

    def send(self, payload):
        self.socket.send(json.dumps(payload))

    def wait_reply(self, request_id, *, start, timeout=REPLY_S):
        return self.until(lambda body: body.get("request_id") == request_id,
                          start=start, description=f"reply to {request_id}", timeout=timeout).body

    def call(self, verb, *, request_id=None, timeout=REPLY_S, **fields):
        request_id = request_id or unique(verb)
        start = len(self.frames)
        self.send({"type": verb, "request_id": request_id, **fields})
        return self.wait_reply(request_id, start=start, timeout=timeout)

    def subscribe(self):
        assert self.receive().body["type"] == "welcome"
        reply = self.call("hello", subscribe={"include_subagents": True, "events_mode": "full"})
        assert reply["type"] == "hello", reply
        snapshot = self.receive().body
        assert snapshot["type"] == "snapshot", snapshot
        return snapshot

    def event(self, stream_id, *, start, match, description, timeout=REPLY_S):
        frame = self.until(
            lambda body: body.get("type") == "chat.event"
            and body["event"]["stream_id"] == stream_id and match(body["event"]),
            start=start, description=description, timeout=timeout,
        )
        return frame.body["event"]

    def history(self, stream_id, *, limit, chunk_limit, **fields):
        request_id = unique("history")
        self.send({"type": "request_stream_events", "request_id": request_id,
                   "stream_id": stream_id, "limit": limit, "chunk_limit": chunk_limit, **fields})
        deadline = time.monotonic() + REPLY_S
        replies = []
        while not replies or replies[-1].body["type"] == "request_stream_events.chunk":
            frame = self.until(lambda body: body.get("request_id") == request_id,
                               start=len(self.frames), description=f"history for {stream_id}",
                               timeout=max(0, deadline - time.monotonic()))
            replies.append(frame)
        assert replies[-1].body["type"] == "request_stream_events.ok", replies[-1].body
        return replies

    def close(self):
        self.socket.close()


@dataclass(frozen=True)
class Seat:
    name: str
    stream_id: str
    generation: str
    transcript: Path

    def record(self, kind, text):
        content = text if kind == "user" else [{"type": "text", "text": text}]
        return {"type": kind, "uuid": str(uuid.uuid4()), "sessionId": self.transcript.stem,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": kind, "content": content}}

    def append(self, *records):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        with self.transcript.open("a", encoding="utf-8") as output:
            output.write("".join(json.dumps(record) + "\n" for record in records))


def read_records(path):
    try:
        lines = path.read_text(encoding="utf-8").split("\n")[:-1]
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line]


def observed(read, matches, *, description, timeout):
    deadline = time.monotonic() + timeout
    while True:
        last = read()
        if matches(last):
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{description} within {timeout}s; last observed {last!r}")
        time.sleep(min(remaining, 0.025))


class Contract:
    def __init__(self, daemon, namespace, control, stack):
        self.daemon = daemon
        self.namespace = namespace
        self.control = control
        self.stack = stack

    def start(self):
        self.daemon.start()
        # The launcher's admission wait historically returns on expiry too.
        with_rpc = self.client()
        try:
            status = with_rpc.call("spawn_status", idempotency_key=unique("probe"))
            assert status["type"] == "spawn_status.ok", status
        finally:
            with_rpc.close()

    def client(self, **kwargs):
        client = Client(self.daemon.url, **kwargs)
        self.stack.callback(client.close)
        return client

    def spawn(self, client, name, *, key=None, **fields):
        payload = spawn_payload(name, "", key or unique("spawn-key"))
        payload.pop("type")
        payload.pop("initial_prompt")
        payload.update(fields)
        reply = client.call("spawn", timeout=SPAWN_S, **payload)
        stream_id = f"{LOCAL_HOST}:{name}"
        assert reply["type"] == "spawn.ok" and reply["stream_id"] == stream_id, reply
        row = self.inspect(client, stream_id, event_tail=0)["session"]
        assert row["status"] == "open" and row["session_generation"], row
        relative = Path(row["jsonl_path"]).resolve().relative_to(self.daemon.projects_root)
        transcript = self.daemon.projects_root / relative
        return Seat(name, stream_id, row["session_generation"], transcript)

    def inspect(self, client, stream_id, **fields):
        reply = client.call("inspect_stream", stream_id=stream_id, **fields)
        assert reply["type"] == "inspect_stream.ok", reply
        assert reply["stream_id"] == stream_id
        return reply

    def token(self, seat):
        key = hashlib.sha256(seat.stream_id.encode()).hexdigest()[:24]
        path = self.daemon.spawn_cwd / ".pentacle-stream-tokens" / f"{key}.token"
        assert not path.is_symlink() and path.stat().st_mode & 0o077 == 0
        return path.read_text(encoding="utf-8").strip()

    def close_seat(self, client, seat):
        return client.call("close", stream_id=seat.stream_id, from_stream_id=seat.stream_id,
                           stream_token=self.token(seat), expected_generation=seat.generation)

    def panes(self, seat):
        reply = self.namespace.run("list-panes", "-t", f"={seat.name}:",
                                   "-F", "#{pane_id} #{pane_pid}")
        assert reply.returncode == 0, reply.stderr
        return reply.stdout.splitlines()

    def inputs(self, seat):
        return read_records(self.control / f"{seat.name}.inputs")

    def hold_user(self):
        (self.control / "mode").write_text("withhold_user", encoding="utf-8")

    def release_user(self):
        (self.control / "release").touch()


def event_stamp(event):
    return event["raw"]["jsonl_record_uuid"], event["raw"]["jsonl_event_index"]


def expect_landed(reply, stream_id, request_id):
    assert (reply["type"], reply["request_id"], reply.get("to_stream_id"),
            reply.get("delivery"), reply.get("submission_confirmed")) == (
                "send.result", request_id, stream_id, "landed", True), reply
