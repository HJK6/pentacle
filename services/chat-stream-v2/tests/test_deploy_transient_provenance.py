"""Real census interleaving, with only ps/launchd/log counterparts substituted."""
import os
import subprocess

import pytest

from deploy import coordinated as c, deploy as d
from deploy.coordinated_runtime import Runtime

DAEMON = 16665
CHILD = 10095
BIRTH = "Thu Nov 5 08:55:16 2026"
ABSENT = (1, "", "")


class Processes:
    def __init__(self, repo, *, first=ABSENT, confirmation=ABSENT, daemon_birth=(0, BIRTH, ""), helper=False):
        self.repo = repo
        self.first, self.confirmation, self.daemon_birth = first, confirmation, daemon_birth
        self.helper = helper
        self.calls = []
        self.loaded = True
        self.child_reads = 0

    def __call__(self, argv, cwd):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[:2] == ("launchctl", "print"):
            result = (0, f"pid = {DAEMON}\n", "") if self.loaded else (113, "", "Could not find service")
        elif argv[:2] == ("launchctl", "bootout"):
            self.loaded = False
            result = (0, "", "")
        elif argv == ("ps", "-ax", "-o", "pid=,ppid=,uid=,command="):
            uid = os.getuid()
            child_parent = 1 if self.helper else DAEMON
            command = f"python {self.repo}/services/chat-stream-v2/provider_account_host.py" if self.helper else "(Python)"
            result = (0, f"{DAEMON} 1 {uid} daemon\n{CHILD} {child_parent} {uid} {command}\n", "") if self.loaded else (0, "", "")
        elif argv[:4] == ("env", "TZ=UTC0", "LC_ALL=C", "ps"):
            assert argv[4:] in (("-p", str(DAEMON), "-o", "lstart="), ("-p", str(CHILD), "-o", "lstart="))
            if int(argv[5]) == DAEMON:
                result = self.daemon_birth if self.loaded else ABSENT
            else:
                self.child_reads += 1
                result = self.first if self.child_reads == 1 else self.confirmation
        else:
            raise AssertionError(f"Unexpected OS operation: {argv}")
        return subprocess.CompletedProcess(argv, *result)


def runtime(tmp_path, processes):
    return Runtime(d, d.SERVICES["chat-streamd-v2"], tmp_path, processes,
                   stream="fixture:deployer", boot_wait_seconds=1)


@pytest.mark.parametrize("helper", [False, True])
def test_provenance_omits_only_independently_absent_non_daemon(tmp_path, helper):
    # Captured live row10095/16665/(Python); its birth and a separate strict
    # absence read return rc1 with empty stdout/stderr. No invented birth.
    processes = Processes(tmp_path, helper=helper)
    proof = runtime(tmp_path, processes).provenance({})
    assert proof == {"launchd_answered": True, "pid": DAEMON,
                     "processes": [{"pid": DAEMON, "start": BIRTH}]}
    assert processes.child_reads == 2
    assert not any(argv[:2] == ("launchctl", "bootout") for argv in processes.calls)


@pytest.mark.parametrize("confirmation", [
    (0, "", ""), (2, "", "ps failed"), (1, "", "permission denied"),
    (1, "not empty", ""), (0, "Thu Nov 5 09:00:00 2026", ""),
])
def test_provenance_refuses_ambiguous_error_or_reappeared_child(tmp_path, confirmation):
    processes = Processes(tmp_path, confirmation=confirmation)
    with pytest.raises(c.Invalid, match="helper process provenance is unproven"):
        runtime(tmp_path, processes).provenance({})
    assert not any(argv[:2] == ("launchctl", "bootout") for argv in processes.calls)


def test_provenance_always_requires_daemon_birth(tmp_path):
    processes = Processes(tmp_path, daemon_birth=ABSENT)
    with pytest.raises(c.Invalid, match="helper process provenance is unproven"):
        runtime(tmp_path, processes).provenance({})
    assert processes.child_reads == 0


def test_provenance_retains_a_live_child_birth(tmp_path):
    processes = Processes(tmp_path, first=(0, "child-birth", ""))
    assert runtime(tmp_path, processes).provenance({})["processes"] == [
        {"pid": DAEMON, "start": BIRTH}, {"pid": CHILD, "start": "child-birth"}]
    assert processes.child_reads == 1


@pytest.mark.parametrize("prior_provenance", [None, {"processes": [{"pid": CHILD, "start": "known-earlier-birth"}]}])
def test_stop_does_not_track_an_already_absent_child_without_a_birth(tmp_path, monkeypatch, prior_provenance):
    # Same-class sweep RED: stop() currently keeps CHILD:None forever and
    # refuses after bootout, even when every post-stop reader proves absence.
    processes = Processes(tmp_path)
    rt = runtime(tmp_path, processes)
    monkeypatch.setattr(d, "_daemon_log_paths", lambda service: ())
    monkeypatch.setattr(d, "RELOAD_UNLOAD_WAIT_S", 0.02)
    rt.stop({}, provenance=prior_provenance)
    assert rt.stopped and rt.old_pid == DAEMON and rt.old_start == BIRTH
    assert processes.calls.count(("launchctl", "bootout", d._launchctl_target(rt.service.launchd_label))) == 1


@pytest.mark.parametrize("confirmation", [(0, "", ""), (2, "", "ps failed"), (1, "", "denied"), (0, "reappeared-birth", "")])
def test_stop_ambiguous_child_birth_still_refuses(tmp_path, monkeypatch, confirmation):
    processes = Processes(tmp_path, confirmation=confirmation)
    monkeypatch.setattr(d, "_daemon_log_paths", lambda service: ())
    monkeypatch.setattr(d, "RELOAD_UNLOAD_WAIT_S", 0.02)
    with pytest.raises(c.Invalid):
        runtime(tmp_path, processes).stop({})
    assert not any(argv[:2] == ("launchctl", "bootout") for argv in processes.calls)


def test_stop_always_requires_daemon_birth(tmp_path):
    processes = Processes(tmp_path, daemon_birth=ABSENT)
    with pytest.raises(c.Invalid, match="old daemon process provenance is unproven"):
        runtime(tmp_path, processes).stop({})
    assert not any(argv[:2] == ("launchctl", "bootout") for argv in processes.calls)


@pytest.mark.parametrize("survivor", ["tracked_birth", "old_tree_helper"])
def test_post_stop_survivor_checks_are_preserved(tmp_path, monkeypatch, survivor):
    processes = Processes(tmp_path, first=(0, "child-birth", ""), confirmation=(0, "child-birth", ""))
    def runner(argv, cwd):
        if survivor == "old_tree_helper" and not processes.loaded and tuple(argv) == ("ps", "-ax", "-o", "pid=,ppid=,uid=,command="):
            return subprocess.CompletedProcess(argv, 0,
                f"{CHILD} 1 {os.getuid()} python {tmp_path}/provider_account_host.py\n", "")
        return processes(argv, cwd)
    monkeypatch.setattr(d, "_daemon_log_paths", lambda service: ())
    monkeypatch.setattr(d, "RELOAD_UNLOAD_WAIT_S", 0.02)
    rt = runtime(tmp_path, runner)
    with pytest.raises(c.Invalid, match="local old-tree helper/daemon exit was not proven"):
        rt.stop({})
    assert not rt.stopped


def test_provenance_ownership_selection_is_unchanged(tmp_path):
    processes = Processes(tmp_path, first=(0, "child-birth", ""))
    def runner(argv, cwd):
        result = processes(argv, cwd)
        if tuple(argv) == ("ps", "-ax", "-o", "pid=,ppid=,uid=,command="):
            result.stdout += (f"800 {DAEMON} -2 foreign\n"
                              f"801 1 {os.getuid()} unrelated\n"
                              f"802 1 {os.getuid()} python /other/provider_account_host.py\n")
        return result
    assert runtime(tmp_path, runner).provenance({})["processes"] == [
        {"pid": DAEMON, "start": BIRTH}, {"pid": CHILD, "start": "child-birth"}]
