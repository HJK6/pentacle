"""Small stdlib-only machine sampler shared by the daemon and satellites."""

from __future__ import annotations

import math
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path


STATS_INTERVAL_S = 30.0
WIRE_VERSION = 1
STATS_FIELDS = (
    "cpu_load_1m",
    "memory_used_bytes",
    "memory_total_bytes",
    "disk_used_bytes",
    "disk_total_bytes",
    "uptime_seconds",
)


def _sysctl_int(name: str) -> int:
    try:
        return int(subprocess.check_output(["sysctl", "-n", name], text=True, timeout=2).strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _darwin_available_bytes() -> int:
    page_size = _sysctl_int("hw.pagesize") or 4096
    try:
        output = subprocess.check_output(["vm_stat"], text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return 0
    available_pages = 0
    for line in output.splitlines():
        key, _, value = line.partition(":")
        if key.strip() not in {"Pages free", "Pages inactive", "Pages speculative", "Pages purgeable", "File-backed pages"}:
            continue
        try:
            available_pages += int(value.strip().rstrip("."))
        except ValueError:
            continue
    return available_pages * page_size


def _memory_bytes() -> tuple[int, int]:
    if Path("/proc/meminfo").is_file():
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value.split()[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total:
            return max(0, total - available), total
    total = _sysctl_int("hw.memsize") or 0
    if not total:
        try:
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, OSError, ValueError):
            total = 0
    available = _darwin_available_bytes() if platform.system() == "Darwin" else 0
    return max(0, total - available), total


def _uptime_seconds() -> int:
    try:
        return max(0, int(float(Path("/proc/uptime").read_text().split()[0])))
    except (FileNotFoundError, IndexError, ValueError, OSError):
        pass
    if platform.system() == "Darwin":
        try:
            raw = subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True, timeout=2)
            boot = int(raw.split("sec =", 1)[1].split(",", 1)[0].strip())
            return max(0, int(time.time()) - boot)
        except (IndexError, ValueError, OSError, subprocess.SubprocessError):
            pass
    clock = getattr(time, "CLOCK_BOOTTIME", getattr(time, "CLOCK_MONOTONIC", None))
    return max(0, int(time.clock_gettime(clock))) if clock is not None else 0


def sample_machine_stats(host: str) -> dict[str, int | float | str]:
    """Return the six footer facts for ``host`` without third-party probes."""
    try:
        load = float(os.getloadavg()[0])
    except (AttributeError, OSError):
        load = 0.0
    memory_used, memory_total = _memory_bytes()
    disk = shutil.disk_usage(Path.home())
    return {
        "host": str(host),
        "cpu_load_1m": load if math.isfinite(load) and load >= 0 else 0.0,
        "memory_used_bytes": memory_used,
        "memory_total_bytes": memory_total,
        "disk_used_bytes": disk.used,
        "disk_total_bytes": disk.total,
        "uptime_seconds": _uptime_seconds(),
    }


def validate_machine_stats(stats: object, host: str) -> dict[str, int | float | str] | None:
    if (
        not isinstance(host, str)
        or not host
        or not isinstance(stats, dict)
        or not isinstance(stats.get("host"), str)
        or not stats["host"]
        or stats["host"] != host
    ):
        return None
    result: dict[str, int | float | str] = {"host": host}
    for field in STATS_FIELDS:
        value = stats.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)) or value < 0:
            return None
        result[field] = value
    if result["memory_total_bytes"] <= 0 or result["disk_total_bytes"] <= 0:
        return None
    if result["memory_used_bytes"] > result["memory_total_bytes"]:
        return None
    if result["disk_used_bytes"] > result["disk_total_bytes"]:
        return None
    return result
