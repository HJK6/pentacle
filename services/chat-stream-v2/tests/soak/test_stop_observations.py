"""`RestartDaemon.stop` observes the S3a barrier on both sides of delivery.

Subject: the `before_signal` and `after_exit` hooks that S3a uses for
`held_through_signal` and `barrier_released_by_cancellation`. Process actions
are stubbed (no child is started or signalled), so each legal schedule is
modelled exactly: a daemon that reaps its wrapper before the signalling thread
resumes, one that reaps it after, and one that exits leaving it to group
cleanup. The existing post-signal `on_signal` hook keeps its position.
"""

from __future__ import annotations

import os
import signal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.soak import restart_matrix as matrix


def stopped(*, daemon_reaps: str):
    """Run stop() over a stub daemon whose wrapper ends as `daemon_reaps` says."""
    daemon = matrix.RestartDaemon.__new__(matrix.RestartDaemon)
    wrapper = {"alive": True}
    order: list[str] = []

    def wait(timeout):
        order.append("exit")
        if daemon_reaps == "after-resume":
            wrapper["alive"] = False
        return 0

    daemon.proc = SimpleNamespace(pid=888, returncode=0, wait=wait)

    def deliver(pid, sig):
        assert pid == 888 and sig == signal.SIGTERM
        order.append("signal")
        if daemon_reaps == "before-resume":
            wrapper["alive"] = False

    def group_cleanup(group, sig):
        assert group == 123 and sig == signal.SIGKILL
        order.append("killpg")
        wrapper["alive"] = False

    with patch.object(os, "getpgid", return_value=123), patch.object(os, "kill", side_effect=deliver), \
            patch.object(os, "killpg", side_effect=group_cleanup):
        result = daemon.stop(
            signal.SIGTERM,
            on_signal=lambda: order.append("on_signal") or {"post": True},
            before_signal=lambda: {"barrier_held": wrapper["alive"]},
            after_exit=lambda: {"barrier_gone": not wrapper["alive"]},
        )
    return result, order


@pytest.mark.parametrize("daemon_reaps", ["before-resume", "after-resume"])
def test_a_healthy_drain_passes_both_observations_in_either_schedule(daemon_reaps: str) -> None:
    result, order = stopped(daemon_reaps=daemon_reaps)
    assert result["before_signal"] == {"barrier_held": True}
    assert result["after_exit"] == {"barrier_gone": True}
    assert result["at_signal"] == {"post": True}
    assert order == ["signal", "on_signal", "exit", "killpg"]
    assert result["rc"] == 0 and not result["escalated_to_sigkill"]


def test_a_wrapper_left_to_group_cleanup_fails_the_reap_observation() -> None:
    result, order = stopped(daemon_reaps="never")
    assert result["before_signal"] == {"barrier_held": True}
    assert result["after_exit"] == {"barrier_gone": False}
    assert order[-1] == "killpg"


def test_cleanup_runs_even_when_an_observation_fails() -> None:
    daemon = matrix.RestartDaemon.__new__(matrix.RestartDaemon)
    daemon.proc = SimpleNamespace(pid=888, returncode=None, wait=lambda timeout: 0)
    killed: list[tuple[int, int]] = []

    def broken():
        raise RuntimeError("observation failed")

    def kill(pid, sig):
        killed.append((pid, sig))
        daemon_proc.returncode = -9

    daemon_proc = daemon.proc
    with patch.object(os, "getpgid", return_value=123), patch.object(os, "kill", side_effect=kill), \
            patch.object(os, "killpg") as killpg, pytest.raises(RuntimeError):
        daemon.stop(signal.SIGTERM, before_signal=broken)
    assert killed == [(888, signal.SIGKILL)]
    killpg.assert_called_once_with(123, signal.SIGKILL)
    assert daemon.proc is None
