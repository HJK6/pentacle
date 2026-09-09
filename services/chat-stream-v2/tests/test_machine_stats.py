from __future__ import annotations

import shutil

import machine_stats


def test_sample_machine_stats_has_sane_footer_fields() -> None:
    sample = machine_stats.sample_machine_stats("testhost")

    assert sample["host"] == "testhost"
    assert set(machine_stats.STATS_FIELDS) <= sample.keys()
    assert isinstance(sample["cpu_load_1m"], float)
    assert sample["cpu_load_1m"] >= 0
    assert 0 < sample["memory_total_bytes"]
    assert 0 <= sample["memory_used_bytes"] <= sample["memory_total_bytes"]
    assert 0 < sample["disk_total_bytes"]
    assert 0 <= sample["disk_used_bytes"] <= sample["disk_total_bytes"]
    assert sample["uptime_seconds"] >= 0


def test_sample_machine_stats_is_deterministic_over_stdlib_inputs(monkeypatch) -> None:
    monkeypatch.setattr(machine_stats.os, "getloadavg", lambda: (1.25, 0.5, 0.25))
    monkeypatch.setattr(machine_stats, "_memory_bytes", lambda: (20, 100))
    monkeypatch.setattr(machine_stats, "_uptime_seconds", lambda: 42)
    monkeypatch.setattr(
        machine_stats.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=1000, used=250, free=750),
    )

    assert machine_stats.sample_machine_stats("hostc") == {
        "host": "hostc",
        "cpu_load_1m": 1.25,
        "memory_used_bytes": 20,
        "memory_total_bytes": 100,
        "disk_used_bytes": 250,
        "disk_total_bytes": 1000,
        "uptime_seconds": 42,
    }


def test_validate_machine_stats_rejects_missing_or_non_numeric_fields() -> None:
    sample = machine_stats.sample_machine_stats("hosta")
    assert machine_stats.validate_machine_stats(sample, "hosta") == sample
    missing_host = dict(sample)
    missing_host.pop("host")
    assert machine_stats.validate_machine_stats(missing_host, "hosta") is None
    assert machine_stats.validate_machine_stats({**sample, "host": ""}, "hosta") is None
    assert machine_stats.validate_machine_stats({**sample, "host": "hostc"}, "hosta") is None
    missing = dict(sample)
    missing.pop("disk_total_bytes")
    assert machine_stats.validate_machine_stats(missing, "hosta") is None
    boolean = {**sample, "cpu_load_1m": False}
    assert machine_stats.validate_machine_stats(boolean, "hosta") is None
