"""Read the daemon's welcome frame once and print its runtime_sha."""
import argparse
import asyncio
import json
import sys

import websockets


async def probe(url: str, timeout: float = 5.0) -> int:
    try:
        async with websockets.connect(url, open_timeout=timeout) as ws:
            for _ in range(5):
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if frame.get("type") == "welcome":
                    print(frame.get("runtime_sha", ""))
                    return 0
    except Exception as exc:
        print(f"unavailable: {type(exc).__name__}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="daemon WebSocket URL")
    args = parser.parse_args()
    return asyncio.run(probe(args.url))


if __name__ == "__main__":
    raise SystemExit(main())
