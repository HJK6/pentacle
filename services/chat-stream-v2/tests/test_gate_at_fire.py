"""Scheduled tests gate at fire: run only on a bound FD approval, else a visible skip, never a pass."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import gate_at_fire as gaf

_REAL_WS_NOTIFY = gaf.ws_notify


@pytest.fixture(autouse=True)
def _no_real_daemon(monkeypatch):
    """Never reach a real daemon or assistant binding from a test."""
    monkeypatch.setattr(gaf, "ws_notify", lambda *a, **k: None)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "candidate"
    path.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=x", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True)
    return path


def _run(tmp, repo, marker, *extra, wait="1.5", job="nightly-soak"):
    return gaf.main(["run", "--job", job, "--duration", "~30 min", "--wait-s", wait, "--poll-s", "0.05",
                     "--state-dir", str(tmp / "state"), "--candidate-repo", str(repo), *extra, "--",
                     sys.executable, "-c", f"open({str(marker)!r},'w').write('ran')"])


def _approve_when_pending(tmp, reservations="none", mutate=None):
    state = tmp / "state"

    def go():
        for _ in range(200):
            found = list((state / "pending").glob("*.json")) if (state / "pending").exists() else []
            if found:
                if mutate:
                    mutate()
                gaf.main(["approve", found[0].stem, "--reservations", reservations, "--state-dir", str(state)])
                return
            time.sleep(0.02)
    t = threading.Thread(target=go)
    t.start()
    return t


def _last(tmp, job="nightly-soak"):
    return json.loads((tmp / "state" / f"last-{job}.json").read_text())


def test_no_approval_skips_visibly_and_never_runs(tmp_path, repo, capsys):
    marker = tmp_path / "ran"
    assert _run(tmp_path, repo, marker, wait="0.3") == gaf.SKIPPED_RC
    assert not marker.exists()
    assert "SKIPPED_UNAPPROVED" in capsys.readouterr().out
    assert _last(tmp_path)["outcome"] == "SKIPPED_UNAPPROVED"


def test_late_approval_cannot_revive_an_expired_occurrence(tmp_path, repo):
    _run(tmp_path, repo, tmp_path / "ran", wait="0.2")
    run_id = next((tmp_path / "state" / "expired").iterdir()).name
    assert gaf.main(["approve", run_id, "--reservations", "none", "--state-dir", str(tmp_path / "state")]) == 1
    assert not (tmp_path / "state" / "approved" / run_id).exists()


def test_approval_after_the_deadline_is_refused_even_before_the_expiry_marker(tmp_path, repo):
    state = tmp_path / "state"
    gaf._write(state / "pending" / "j-1.json", {"run_id": "j-1", "host": "h", "candidate": "c", "deadline_at": time.time() - 1})
    assert gaf.main(["approve", "j-1", "--reservations", "none", "--state-dir", str(state)]) == 1


def test_an_approval_that_lands_first_wins_over_expiry(tmp_path):
    state = tmp_path / "state"
    gaf._write(state / "approved" / "j-1", {"run_id": "j-1"})
    assert gaf._expire(state, "j-1") is False
    assert not (state / "expired" / "j-1").exists()


def test_approve_requires_the_fd_reservation_snapshot(tmp_path, repo):
    state = tmp_path / "state"
    gaf._write(state / "pending" / "j-1.json", {"run_id": "j-1", "host": "h", "candidate": "c", "deadline_at": time.time() + 60})
    assert gaf.main(["approve", "j-1", "--state-dir", str(state)]) == 1
    assert gaf.main(["approve", "j-1", "--reservations", "mobile quiet 14-16Z", "--state-dir", str(state)]) == 0
    assert json.loads((state / "approved" / "j-1").read_text())["reservations"] == "mobile quiet 14-16Z"


def test_approved_occurrence_runs_consumes_the_grant_and_records_duration(tmp_path, repo):
    marker = tmp_path / "ran"
    t = _approve_when_pending(tmp_path, reservations="none")
    assert _run(tmp_path, repo, marker) == 0
    t.join()
    assert marker.read_text() == "ran"
    last = _last(tmp_path)
    assert last["outcome"] == "RAN" and last["rc"] == 0 and last["duration_s"] >= 0
    assert list((tmp_path / "state" / "approved").iterdir()) == []  # a grant is single-use
    assert list((tmp_path / "state" / "running").iterdir()) == []


def test_candidate_changed_after_approval_needs_a_fresh_gate(tmp_path, repo, capsys):
    marker = tmp_path / "ran"

    def move_head():
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=x", "commit", "-q",
                        "--allow-empty", "-m", "moved"], check=True)
    t = _approve_when_pending(tmp_path, mutate=move_head)
    assert _run(tmp_path, repo, marker) == gaf.SKIPPED_RC
    t.join()
    assert not marker.exists()
    assert "SKIPPED_CANDIDATE_CHANGED" in capsys.readouterr().out


def test_a_dirty_candidate_is_refused_with_no_gate_and_no_run(tmp_path, repo, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(gaf, "ws_notify", lambda *a, **k: sent.append(a))
    (repo / "code.py").write_text("untracked")
    marker = tmp_path / "ran"
    assert _run(tmp_path, repo, marker, wait="0.1") == gaf.REFUSED_RC
    assert sent == [] and not marker.exists()
    assert "REFUSED" in capsys.readouterr().out and "dirty" in _last(tmp_path)["why"]


def test_a_candidate_dirtied_after_approval_is_not_run(tmp_path, repo, capsys):
    marker = tmp_path / "ran"
    t = _approve_when_pending(tmp_path, mutate=lambda: (repo / "code.py").write_text("changed after approval"))
    assert _run(tmp_path, repo, marker) == gaf.SKIPPED_RC
    t.join()
    assert not marker.exists() and "SKIPPED_CANDIDATE_CHANGED" in capsys.readouterr().out


def _machines(tmp_path, monkeypatch, names=("alpha",)):
    path = tmp_path / "machines.json"
    path.write_text(json.dumps({"machines": [{"name": n} for n in names]}))
    monkeypatch.setenv("PENTACLE_MACHINES_FILE", str(path))
    monkeypatch.delenv("PENTACLE_MACHINES_JSON", raising=False)
    return path


def _run_printing_machines(tmp, repo, out):
    code = f"import os;open({str(out)!r},'w').write(os.environ['PENTACLE_MACHINES_FILE']+'|'+open(os.environ['PENTACLE_MACHINES_FILE']).read())"
    return gaf.main(["run", "--job", "spawn-fleet-smoke", "--duration", "x", "--wait-s", "5", "--poll-s", "0.05",
                     "--state-dir", str(tmp / "state"), "--candidate-repo", str(repo), "--targets", "@machines",
                     "--", sys.executable, "-c", code])


def test_the_approved_run_uses_the_captured_target_list_not_a_fresh_reload(tmp_path, repo, monkeypatch):
    machines = _machines(tmp_path, monkeypatch, ("alpha",))
    out = tmp_path / "seen"
    t = _approve_when_pending(tmp_path)
    assert _run_printing_machines(tmp_path, repo, out) == 0
    t.join()
    path, content = out.read_text().split("|", 1)
    assert Path(path).parent == tmp_path / "state" / "machines" and path != str(machines)
    assert json.loads(content) == {"machines": [{"name": "alpha"}]}


def test_a_target_list_edited_after_the_gate_is_skipped_visibly(tmp_path, repo, monkeypatch, capsys):
    machines = _machines(tmp_path, monkeypatch, ("alpha",))
    out = tmp_path / "seen"
    t = _approve_when_pending(tmp_path, mutate=lambda: machines.write_text(json.dumps({"machines": [{"name": "beta"}]})))
    assert _run_printing_machines(tmp_path, repo, out) == gaf.SKIPPED_RC
    t.join()
    assert not out.exists() and "SKIPPED_CANDIDATE_CHANGED" in capsys.readouterr().out


def test_inline_machines_json_override_makes_the_target_list_ambiguous(tmp_path, repo, monkeypatch):
    _machines(tmp_path, monkeypatch)
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", "{}")
    assert _run_printing_machines(tmp_path, repo, tmp_path / "seen") == gaf.REFUSED_RC


def test_the_single_deadline_bounds_sleeps_and_rejects_nonpositive_polls(tmp_path, repo, capsys):
    began = time.monotonic()
    assert _run(tmp_path, repo, tmp_path / "ran", "--poll-s", "5", wait="0.2") == gaf.SKIPPED_RC
    assert time.monotonic() - began < 1.5  # not one full 5 s poll past the 0.2 s deadline
    assert _run(tmp_path, repo, tmp_path / "ran", "--poll-s", "0", wait="0.2") == gaf.REFUSED_RC


class _SlowConn:
    """Fake connection whose first RPC consumes the notification budget."""

    def __init__(self, burn):
        self.burn, self.rpcs, self.timeout = burn, [], None

    def rpc(self, payload):
        self.rpcs.append((payload["type"], self.timeout))
        if len(self.rpcs) == 1:
            time.sleep(self.burn)
            return BINDING
        return {"type": "send.result", "delivery": "landed"}


def test_notification_phases_share_one_absolute_deadline_with_no_floor(monkeypatch, tmp_path):
    from contextlib import contextmanager
    import tools.live_window as lw
    seen = {}
    conn = _SlowConn(burn=0.12)

    @contextmanager
    def connection(_url, _token, timeout):
        seen["connect_timeout"] = timeout
        conn.timeout = timeout
        yield conn

    monkeypatch.setattr(lw, "authenticated_operator_connection", connection)
    deadline = time.monotonic() + 0.08  # subsecond budget; the first phase overruns it
    with pytest.raises(TimeoutError, match="deadline"):
        _REAL_WS_NOTIFY("GATE scheduled-test r", "r", "ws://x", tmp_path / "t", deadline)
    assert seen["connect_timeout"] <= 0.08  # never floored to a fresh 1 s budget
    assert [name for name, _ in conn.rpcs] == ["assistant.binding"]  # no send after the budget is gone
    assert conn.rpcs[0][1] <= 0.08


def test_notification_with_no_budget_left_is_refused_before_any_connection(monkeypatch, tmp_path):
    import tools.live_window as lw
    monkeypatch.setattr(lw, "authenticated_operator_connection", lambda *a: pytest.fail("connected with no budget"))
    with pytest.raises(TimeoutError, match="deadline"):
        _REAL_WS_NOTIFY("GATE scheduled-test r", "r", "ws://x", tmp_path / "t", time.monotonic() - 0.01)


def test_a_slow_notification_cannot_push_the_skip_past_the_deadline(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setattr(gaf, "ws_notify", lambda *a, **k: time.sleep(1.5))
    marker = tmp_path / "ran"
    began = time.monotonic()
    assert _run(tmp_path, repo, marker, wait="0.3") == gaf.SKIPPED_RC
    assert time.monotonic() - began < 1.0  # the 1.5 s notification did not extend the 0.3 s occurrence
    assert not marker.exists()
    out = capsys.readouterr().out
    assert "gate notify failed TimeoutError" in out and "SKIPPED_UNAPPROVED" in out


def test_no_budget_for_the_gate_skips_notification_and_never_launches(tmp_path, repo, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(gaf, "ws_notify", lambda *a, **k: sent.append(a))
    real_targets = gaf.targets
    monkeypatch.setattr(gaf, "targets", lambda raw: (time.sleep(0.15), real_targets(raw))[1])  # eats the budget first
    marker = tmp_path / "ran"
    assert _run(tmp_path, repo, marker, wait="0.1") == gaf.SKIPPED_RC
    assert sent == [] and not marker.exists()
    assert "gate notify skipped" in capsys.readouterr().out


def test_a_consumed_occurrence_cannot_be_approved_again_and_leaves_no_grant(tmp_path, repo):
    t = _approve_when_pending(tmp_path)
    assert _run(tmp_path, repo, tmp_path / "ran") == 0
    t.join()
    state = tmp_path / "state"
    run_id = json.loads((state / "last-nightly-soak.json").read_text())["run_id"]
    assert gaf.main(["approve", run_id, "--reservations", "none", "--state-dir", str(state)]) == 1
    assert list((state / "approved").iterdir()) == []


def test_run_ids_are_collision_resistant_per_occurrence(tmp_path, repo):
    _run(tmp_path, repo, tmp_path / "r", wait="0.1")
    _run(tmp_path, repo, tmp_path / "r", wait="0.1")
    assert len(list((tmp_path / "state" / "expired").iterdir())) == 2


def test_wait_cannot_exceed_thirty_minutes_and_nothing_is_sent(tmp_path, repo, monkeypatch):
    sent = []
    monkeypatch.setattr(gaf, "ws_notify", lambda *a, **k: sent.append(a))
    assert _run(tmp_path, repo, tmp_path / "ran", wait="1801") == gaf.REFUSED_RC
    assert sent == []


def test_unresolved_candidate_or_targets_fail_closed_without_a_gate(tmp_path, repo, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(gaf, "ws_notify", lambda *a, **k: sent.append(a))
    assert _run(tmp_path, tmp_path / "not-a-repo", tmp_path / "ran", wait="0.1") == gaf.REFUSED_RC
    monkeypatch.setenv("PENTACLE_MACHINES_FILE", str(tmp_path / "missing.json"))
    assert _run(tmp_path, repo, tmp_path / "ran", "--targets", "@machines", wait="0.1") == gaf.REFUSED_RC
    assert sent == [] and "REFUSED" in capsys.readouterr().out
    assert _last(tmp_path)["outcome"] == "REFUSED"


def test_gate_names_every_required_field_with_the_full_candidate(tmp_path, repo, capsys, monkeypatch):
    machines = tmp_path / "machines.json"
    machines.write_text(json.dumps({"machines": [{"name": "host-b"}, {"name": "host-c"}, {"name": "host-a"}]}))
    monkeypatch.setenv("PENTACLE_MACHINES_FILE", str(machines))
    _run(tmp_path, repo, tmp_path / "ran", "--targets", "@machines", wait="0.1")
    out = capsys.readouterr().out
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for field in ("run=nightly-soak-", f"candidate={sha}", "candidate_clean=true", "runtime=", "proposed_host=",
                  "target_hosts=host-b,host-c,host-a", "machines_sha256=", "expected_duration=~30 min", "load/cpu=",
                  "known_reservations=NOT KNOWN TO THE JOB", "approve with:", "SKIPPED_UNAPPROVED"):
        assert field in out


def test_verify_accepts_only_a_live_wrapper_occurrence(tmp_path):
    state = tmp_path / "state"
    assert gaf.main(["verify", "x", "--state-dir", str(state)]) == 1
    gaf._write(state / "running" / "x", {"run_id": "x", "pid": 2**22 + 12345, "host": "h"})  # no such process
    assert gaf.main(["verify", "x", "--state-dir", str(state)]) == 1
    import os
    gaf._write(state / "running" / "y", {"run_id": "y", "pid": os.getpid(), "host": "h"})
    assert gaf.main(["verify", "y", "--state-dir", str(state)]) == 0


class _Conn:
    def __init__(self, replies):
        self.replies, self.sent = replies, []

    def rpc(self, payload):
        self.sent.append(payload)
        return self.replies[payload["type"]]


def _patch_connection(monkeypatch, conn):
    from contextlib import contextmanager
    import tools.live_window as lw

    @contextmanager
    def connection(_url, _token, _timeout):
        yield conn

    monkeypatch.setattr(lw, "authenticated_operator_connection", connection)


BINDING = {"type": "assistant.binding.ok", "stream_id": "host-b:v2-fd"}


def test_ws_notify_sends_the_gate_to_the_current_assistant_binding_only(monkeypatch, tmp_path):
    conn = _Conn({"assistant.binding": BINDING, "send": {"type": "send.result", "delivery": "landed"}})
    _patch_connection(monkeypatch, conn)
    _REAL_WS_NOTIFY("GATE scheduled-test run=r1 x", "r1", "ws://x", tmp_path / "t")
    assert [m["type"] for m in conn.sent] == ["assistant.binding", "send"]
    sent = conn.sent[1]
    assert (sent["host"], sent["session_name"]) == ("host-b", "v2-fd") and sent["text"].startswith("GATE scheduled-test ")


@pytest.mark.parametrize("reply", [{"type": "send.error"}, {"type": "send.result", "delivery": "failed"},
                                   {"type": "send.result"}, {"type": "send.result", "delivery": "accepted"}])
def test_ws_notify_accepts_only_a_delivered_send_result(monkeypatch, tmp_path, reply):
    _patch_connection(monkeypatch, _Conn({"assistant.binding": BINDING, "send": reply}))
    with pytest.raises(RuntimeError, match="send refused"):
        _REAL_WS_NOTIFY("GATE scheduled-test r", "r", "ws://x", tmp_path / "t")


def test_ws_notify_refuses_non_gate_text(tmp_path):
    with pytest.raises(ValueError):
        _REAL_WS_NOTIFY("REPORT other", "r", "ws://x", tmp_path / "t")


def test_default_transport_failure_is_logged_and_still_skips_visibly(tmp_path, repo, capsys, monkeypatch):
    monkeypatch.setattr(gaf, "ws_notify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("daemon down")))
    assert _run(tmp_path, repo, tmp_path / "ran", wait="0.2") == gaf.SKIPPED_RC
    out = capsys.readouterr().out
    assert "gate notify failed RuntimeError: daemon down" in out and "SKIPPED_UNAPPROVED" in out


def test_notify_command_failure_still_waits_and_skips(tmp_path, repo, capsys):
    assert _run(tmp_path, repo, tmp_path / "ran", "--notify-cmd", "false", wait="0.2") == gaf.SKIPPED_RC
    assert "gate notify failed" in capsys.readouterr().out


def test_fleet_smoke_launchd_template_is_gated_with_targets_and_a_stated_duration():
    template = Path(__file__).resolve().parents[1] / "deploy" / "com.pentacle.spawn-fleet-smoke.plist"
    text = template.read_text()
    assert "tools/gate_at_fire.py run --job spawn-fleet-smoke" in text
    assert "--targets @machines" in text and "--duration unmeasured" not in text and "--candidate-repo" in text
    assert text.index("gate_at_fire.py") < text.index("spawn_fleet_smoke.py")


def test_the_real_live_window_import_resolves_from_a_bare_interpreter(tmp_path):
    """Found in the live rehearsal on host-b: with only the helper's own paths, tools.live_window needs services/ too.

    Every other test fakes the connection module, so run the real import in a clean interpreter."""
    code = ("import sys; sys.path.insert(0, %r); import gate_at_fire; gate_at_fire._ensure_import_paths(); "
            "import tools.live_window; print('ok')") % str(Path(gate_at_fire_path()).parent)
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-400:]


def gate_at_fire_path():
    return gaf.__file__
