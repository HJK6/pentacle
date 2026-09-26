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


def test_sample_exposes_cpu_utilization_separately_from_load() -> None:
    sample = machine_stats.sample_machine_stats("testhost")
    assert "cpu_usage_pct" in sample
    assert sample["cpu_usage_pct"] is None or 0 <= sample["cpu_usage_pct"] <= 100


def test_sample_machine_stats_is_deterministic_over_stdlib_inputs(monkeypatch) -> None:
    monkeypatch.setattr(machine_stats.os, "getloadavg", lambda: (1.25, 0.5, 0.25))
    monkeypatch.setattr(machine_stats, "_cpu_usage_pct", lambda: 37.5)
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
        "cpu_usage_pct": 37.5,
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


def test_cpu_usage_is_a_measured_system_percent_on_linux_and_darwin(monkeypatch) -> None:
    readings = iter([(100, 80), (200, 100)])
    monkeypatch.setattr(machine_stats.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine_stats, "_linux_cpu_ticks", lambda: next(readings))
    monkeypatch.setattr(machine_stats.time, "sleep", lambda _seconds: None)
    assert machine_stats._cpu_usage_pct() == 80.0

    monkeypatch.setattr(machine_stats.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        machine_stats.subprocess, "check_output",
        lambda *_args, **_kwargs: (
            "CPU usage: 2.00% user, 3.00% sys, 95.00% idle\n"
            "CPU usage: 12.50% user, 7.50% sys, 80.00% idle\n"
        ),
    )
    assert machine_stats._cpu_usage_pct() == 20.0

    readings = iter([(100, 80), (200, 180)])
    monkeypatch.setattr(machine_stats.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine_stats, "_linux_cpu_ticks", lambda: next(readings))
    assert machine_stats._cpu_usage_pct() == 0.0

    readings = iter([(100, 80), (100, 80)])
    monkeypatch.setattr(machine_stats, "_linux_cpu_ticks", lambda: next(readings))
    assert machine_stats._cpu_usage_pct() is None

    monkeypatch.setattr(machine_stats.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(machine_stats.subprocess, "check_output", lambda *_args, **_kwargs: "CPU usage: 0% user, 0% sys, 100% idle\n")
    assert machine_stats._cpu_usage_pct() is None


def test_linux_cpu_ticks_exclude_guest_counters(monkeypatch) -> None:
    monkeypatch.setattr(machine_stats.Path, "read_text", lambda _path: "cpu 10 20 30 40 5 6 7 8 9 10\n")
    assert machine_stats._linux_cpu_ticks() == (126, 45)


def test_cpu_usage_unavailable_and_legacy_wire_never_fabricate_zero(monkeypatch) -> None:
    monkeypatch.setattr(machine_stats, "_cpu_usage_pct", lambda: None)
    sample = machine_stats.sample_machine_stats("hosta")
    assert sample["cpu_usage_pct"] is None
    assert machine_stats.validate_machine_stats(sample, "hosta")["cpu_usage_pct"] is None

    legacy = dict(sample)
    legacy.pop("cpu_usage_pct")
    assert "cpu_usage_pct" not in machine_stats.validate_machine_stats(legacy, "hosta")
    for bad in (-1, 101, 10 ** 1000, float("nan"), True, "20"):
        assert machine_stats.validate_machine_stats({**sample, "cpu_usage_pct": bad}, "hosta")["cpu_usage_pct"] is None
    assert machine_stats.validate_machine_stats({**sample, "cpu_usage_pct": 0}, "hosta")["cpu_usage_pct"] == 0
