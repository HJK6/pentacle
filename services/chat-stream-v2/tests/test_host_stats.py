from __future__ import annotations

import asyncio
import json

from server import Server


def _sample(host: str, load: float) -> dict:
    return {
        "host": host,
        "cpu_load_1m": load,
        "memory_used_bytes": 20,
        "memory_total_bytes": 100,
        "disk_used_bytes": 200,
        "disk_total_bytes": 1000,
        "uptime_seconds": 42,
    }


def test_merge_keeps_latest_per_host_broadcasts_full_fleet_and_replays_on_hello() -> None:
    async def run() -> None:
        server = Server(local_host="hosta")
        frames: list[dict] = []

        async def broadcast(frame: dict) -> None:
            frames.append(frame)

        server.broadcast = broadcast
        await server.merge_host_stats("hosta", _sample("hosta", 1.0))
        await server.merge_host_stats("hostc", _sample("hostc", 2.0))
        await server.merge_host_stats("hosta", _sample("hosta", 3.0))

        assert len(frames) == 3
        assert frames[-1]["type"] == "hosts.stats"
        assert frames[-1]["hosts"]["hosta"]["cpu_load_1m"] == 3.0
        assert set(frames[-1]["hosts"]) == {"hosta", "hostc"}
        assert frames[-1]["hosts"]["hosta"]["sampled_at"].endswith("Z")

        hello = await server._on_hello({})
        assert hello[0]["type"] == "hello"
        assert hello[1]["type"] == "snapshot"
        assert hello[2] == frames[-1]

    asyncio.run(run())


def test_host_stats_frame_is_empty_before_the_first_sample() -> None:
    frames = asyncio.run(Server(local_host="hosta")._on_hello({}))
    assert frames[-1] == {"type": "hosts.stats", "hosts": {}}


def test_hello_hosts_stats_replay_cannot_follow_a_newer_broadcast() -> None:
    async def run() -> None:
        class Websocket:
            remote_address = ("127.0.0.1", 54321)
            def __init__(self) -> None:
                self.sent: list[dict] = []
                self.new_sample_sent = asyncio.Event()

            async def send(self, raw: str) -> None:
                frame = json.loads(raw)
                self.sent.append(frame)
                hosts = frame.get("hosts") if isinstance(frame, dict) else None
                hosta = hosts.get("hosta") if isinstance(hosts, dict) else None
                if frame.get("type") == "hosts.stats" and hosta and hosta.get("cpu_load_1m") == 2.0:
                    self.new_sample_sent.set()

        server = Server(local_host="hosta")
        server._host_stats["hosta"] = {
            **_sample("hosta", 1.0),
            "sampled_at": "2026-09-05T00:00:00Z",
        }
        websocket = Websocket()
        server._register_client(websocket)
        old_replay = server.hosts_stats_frame()
        dispatch_started = asyncio.Event()
        release_dispatch = asyncio.Event()

        async def delayed_dispatch(_raw: str, *, websocket: object = None) -> list[dict]:
            dispatch_started.set()
            await release_dispatch.wait()
            return [{"type": "hello"}, {"type": "snapshot"}, old_replay]

        server._dispatch = delayed_dispatch  # type: ignore[method-assign]
        serve_task = asyncio.create_task(
            server._serve(websocket, json.dumps({"type": "hello"}))
        )
        await dispatch_started.wait()

        merge_task = asyncio.create_task(server.merge_host_stats("hosta", _sample("hosta", 2.0)))
        await asyncio.sleep(0)
        if merge_task.done():
            # On the unfixed server, the newer broadcast is free to overtake
            # the paused hello reply; wait until the fake writer observes it.
            await asyncio.wait_for(websocket.new_sample_sent.wait(), timeout=1.0)

        release_dispatch.set()
        await serve_task
        await merge_task
        await asyncio.wait_for(websocket.new_sample_sent.wait(), timeout=1.0)

        samples = [
            frame["hosts"]["hosta"]["cpu_load_1m"]
            for frame in websocket.sent
            if frame.get("type") == "hosts.stats"
        ]
        assert samples == [1.0, 2.0]
        server._unregister_client(websocket)

    asyncio.run(run())
