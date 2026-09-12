#!/usr/bin/env python3
"""Read the Codex CLI weekly rate-limit usage via the app-server JSON-RPC protocol.

Recent Codex releases removed the ``/status`` command, so screen-scraping no
longer works. Instead we spawn ``codex app-server`` (stdio JSON-RPC), complete
the initialize handshake, and call ``account/rateLimits/read`` for structured
rate-limit data, emitting the single weekly Codex limit consumed by the usage
collector.

Usage:
    python3 check_codex_usage.py          # human-readable
    python3 check_codex_usage.py --json   # canonical wire-shape JSON

The ``--json`` object is exactly ``{pct, resets_at_iso, resets_text,
upstream_reported_at}`` (the collector's ``_CODEX_USAGE_FIELDS``). ``resets_at_iso``
is authoritative UTC; ``resets_text`` is display-only, rendered in the host's
local timezone. The Codex binary is discovered on ``PATH`` (the same mechanism
the deploy plist relies on) — no hardcoded install path and no dedicated knob.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, tzinfo

RPC_TIMEOUT = 20  # seconds
_WEEKLY_MINS = 10080  # a window at least this long is the weekly limit


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_codex_bin() -> str | None:
    """Locate the Codex CLI on PATH — the same mechanism the deploy plist relies
    on (its PATH includes the Codex install dir). No dedicated env/knob is added:
    production discovery and test injection both go through PATH."""
    return shutil.which("codex")


def collect_usage(
    *,
    codex_bin: str,
    now_fn=_utc_now_iso,
    popen=subprocess.Popen,
    timeout: float = RPC_TIMEOUT,
    tz: tzinfo | None = None,
) -> dict:
    """Start ``codex app-server``, send initialize + rateLimits/read, parse response."""
    proc = popen(
        [codex_bin, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
        text=True,
        bufsize=1,
    )
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"clientInfo": {"name": "pentacle-usage-probe",
                                      "title": "pentacle-usage-probe",
                                      "version": "1.0.0"}},
        })
        init_resp = _read_response(proc, expect_id=0, timeout=timeout)
        if "error" in init_resp:
            raise RuntimeError(f"initialize failed: {init_resp['error']}")

        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send(proc, {
            "jsonrpc": "2.0", "id": 1,
            "method": "account/rateLimits/read", "params": {},
        })
        resp = _read_response(proc, expect_id=1, timeout=timeout)
        upstream_reported_at = now_fn()
        if "error" in resp:
            raise RuntimeError(f"rateLimits/read failed: {resp['error']}")

        return {
            **_parse_rate_limits(resp["result"], tz=tz),
            "upstream_reported_at": upstream_reported_at,
        }
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
            proc.wait(timeout=3)


def _send(proc, msg: dict) -> None:
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _read_response(proc, expect_id, timeout=RPC_TIMEOUT) -> dict:
    """Read JSON-RPC lines until one carries ``expect_id``; discard notifications
    and other-id responses.

    The monotonic deadline is checked between reads. A fully silent server that
    holds stdout open without writing would block in ``readline``; in production
    that case is bounded by the collector's 75s subprocess timeout (see
    ``usage_collector.UsageStateCollector._json``), which records a
    ``provider_error`` and retains the prior Codex value. This mirrors the proven
    shared probe rather than adding a select/reader layer whose buffered-readline
    interaction is itself error-prone.
    """
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"no response for id={expect_id} within {timeout}s")
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"app-server closed stdout before response id={expect_id}")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == expect_id:
            return msg


def _parse_rate_limits(result: dict, *, tz: tzinfo | None = None) -> dict:
    """Return the unique weekly limit from an app-server response.

    ``primary`` and ``secondary`` are classified by duration, not position. A
    missing weekly window is a valid all-null observation; two weekly windows are
    ambiguous and fail closed.
    """
    if not isinstance(result, dict):
        raise ValueError("rateLimits result must be a mapping")
    rate_limits = result.get("rateLimits")
    if rate_limits is None:
        rate_limits = {}
    if not isinstance(rate_limits, dict):
        raise ValueError("rateLimits must be a mapping")

    weekly = []
    for name in ("primary", "secondary"):
        candidate = rate_limits.get(name)
        if candidate is None or not isinstance(candidate, dict):
            continue
        duration = candidate.get("windowDurationMins")
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and duration >= _WEEKLY_MINS
        ):
            weekly.append(candidate)

    if not weekly:
        return {"pct": None, "resets_at_iso": None, "resets_text": None}
    if len(weekly) != 1:
        raise ValueError("expected exactly one weekly rate-limit candidate")

    candidate = weekly[0]
    if "usedPercent" not in candidate:
        raise ValueError("weekly usedPercent is required")
    pct = _pct(candidate["usedPercent"])
    if pct is None:
        return {"pct": None, "resets_at_iso": None, "resets_text": None}
    if "resetsAt" not in candidate:
        raise ValueError("weekly resetsAt is required for numeric usedPercent")
    resets_at_iso, resets_text = _fmt_reset(candidate["resetsAt"], tz=tz)
    return {"pct": pct, "resets_at_iso": resets_at_iso, "resets_text": resets_text}


def _pct(val):
    """Validate and round a percentage; None is a valid null observation."""
    if val is None:
        return None
    if (
        not isinstance(val, (int, float))
        or isinstance(val, bool)
        or not math.isfinite(val)
        or not 0 <= val <= 100
    ):
        raise ValueError("weekly usedPercent must be null or a finite number from 0 to 100")
    return int(round(val))


def _fmt_reset(ts, *, tz: tzinfo | None = None) -> tuple[str | None, str | None]:
    """Validate Unix seconds and return UTC ISO plus local-timezone display text."""
    if ts is None:
        return None, None
    if (
        not isinstance(ts, (int, float))
        or isinstance(ts, bool)
        or not math.isfinite(ts)
    ):
        raise ValueError("weekly resetsAt must be null or finite Unix seconds")
    try:
        utc_dt = datetime.fromtimestamp(ts, timezone.utc)
        # No explicit zone: the host's local zone, resolved DST-correctly for THIS
        # instant via astimezone() (not a single fixed offset captured at "now").
        local_dt = datetime.fromtimestamp(ts, tz) if tz is not None else utc_dt.astimezone()
    except (ValueError, OSError, OverflowError) as exc:
        raise ValueError("weekly resetsAt is outside the supported timestamp range") from exc
    hour12 = local_dt.strftime("%I").lstrip("0") or "12"
    ampm = local_dt.strftime("%p").lower()
    minute = local_dt.minute
    time_part = f"{hour12}{ampm}" if minute == 0 else f"{hour12}:{local_dt.strftime('%M')}{ampm}"
    date_part = local_dt.strftime("%b ") + str(local_dt.day)
    label = getattr(tz, "key", None) or local_dt.tzname() or "local"
    iso = utc_dt.isoformat().replace("+00:00", "Z")
    text = f"{date_part} at {time_part} ({label})"
    return iso, text


def build_usage_payload(usage: dict) -> dict:
    """Return exactly the weekly Codex object consumed by the v2 collector."""
    keys = {"pct", "resets_at_iso", "resets_text", "upstream_reported_at"}
    if not isinstance(usage, dict) or set(usage) != keys:
        raise ValueError("Codex usage payload must contain exactly the weekly limit fields")
    stamp = usage["upstream_reported_at"]
    if not isinstance(stamp, str):
        raise ValueError("upstream_reported_at must be a UTC RFC3339 string")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("upstream_reported_at must be a UTC RFC3339 string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("upstream_reported_at must be UTC")
    return {key: usage[key] for key in ("pct", "resets_at_iso", "resets_text", "upstream_reported_at")}


def format_usage(usage: dict) -> str:
    def bar(pct):
        if pct is None:
            return "N/A"
        filled = min(20, pct // 5)
        return f"[{'#' * filled}{'.' * (20 - filled)}] {pct}%"

    return "\n".join([
        f"Codex weekly: {bar(usage['pct'])} used",
        f"  Resets: {usage.get('resets_text') or 'N/A'}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read Codex weekly usage via app-server JSON-RPC")
    parser.add_argument("--json", action="store_true", help="emit canonical wire-shape JSON")
    args = parser.parse_args(argv)

    codex_bin = _resolve_codex_bin()
    if not codex_bin:
        # Redacted: never echo tokens, pane content, or discovered paths into health UI.
        print("Codex CLI not found on PATH; install it or add it to PATH", file=sys.stderr)
        return 1
    try:
        usage = collect_usage(codex_bin=codex_bin)
    except Exception as exc:  # noqa: BLE001 - one redacted failure path for the collector
        print(f"Codex usage unavailable ({type(exc).__name__}); check Codex login and app-server", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(build_usage_payload(usage)))
    else:
        print(format_usage(usage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
