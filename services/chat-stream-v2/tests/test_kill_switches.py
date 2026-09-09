"""Background-task switches parse; unsupported switches do not."""

from __future__ import annotations

import re
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]

from main import parse_args  # noqa: E402

_SWITCH = re.compile(r"--disable-([a-z][a-z-]*)")
_REMOVED_SWITCHES = frozenset({"reaper", "alerts", "schedule", "usage-probes"})


def _advertised_switches() -> set[str]:
    names: set[str] = set()
    for module in SERVICE_DIR.glob("*.py"):
        names.update(_SWITCH.findall(module.read_text()))
    return names


def test_every_advertised_kill_switch_parses() -> None:
    switches = sorted(_advertised_switches())
    # Sanity: retained non-core switches must be discovered.
    assert {"nudges", "notification-expiry"} <= set(switches)
    # A parser missing any advertised flag raises SystemExit here.
    args = parse_args([f"--disable-{name}" for name in switches])
    for name in switches:
        assert getattr(args, f"disable_{name.replace('-', '_')}") is True


def test_switches_default_off() -> None:
    args = parse_args([])
    for name in _advertised_switches():
        assert getattr(args, f"disable_{name.replace('-', '_')}") is False


def test_removed_task_owner_switches_are_rejected() -> None:
    for name in sorted(_REMOVED_SWITCHES):
        try:
            parse_args([f"--disable-{name}"])
        except SystemExit:
            continue
        raise AssertionError(f"removed task-owner switch still parses: {name}")
