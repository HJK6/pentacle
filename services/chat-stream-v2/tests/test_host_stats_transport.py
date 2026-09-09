from __future__ import annotations

import asyncio

from event_push import EventPush


class _Store:
    async def get(self, _key: str):
        return None


class _Alerts:
    def emit(self, *_args, **_kwargs) -> None:
        pass


def _sample() -> dict:
    return {
        "host": "hostc",
        "cpu_load_1m": 0.5,
        "memory_used_bytes": 100,
        "memory_total_bytes": 1000,
        "disk_used_bytes": 500,
        "disk_total_bytes": 2000,
        "uptime_seconds": 10,
    }


def test_host_stats_uses_satellite_auth_and_daemon_merge_callback() -> None:
    async def run() -> None:
        received: list[tuple[str, dict]] = []

        async def merge(host: str, stats: dict) -> None:
            received.append((host, stats))

        sink = EventPush(
            _Store(), lambda _frame: None, _Alerts(), recent_limit=10,
            host_stats_handler=merge,
        )

        async def secret() -> str:
            return "secret"

        sink._secret = secret  # type: ignore[method-assign]
        reply = await sink.handle_host_stats({
            "type": "host.stats", "request_id": "stats-1", "push_secret": "secret",
            "satellite_sha": "", "host": "hostc", "wire_version": 1, "stats": _sample(),
        })

        assert reply["type"] == "host.stats.ok"
        assert reply["request_id"] == "stats-1"
        assert received == [("hostc", _sample())]

    asyncio.run(run())
