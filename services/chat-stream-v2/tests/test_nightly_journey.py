"""Scheduled admission and reporting through isolated command transports."""
from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import nightly_journey as nj
import scheduled_admission as sa

NOW = "2026-11-15T09:00:00Z"


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setattr(sa.platform, "node", lambda: "test-host.local")
    path = tmp_path / "window.json"
    path.write_text(json.dumps(dict(host="test-host", start_utc="05:30", duration_s=21600,
                                   valid_until="2026-11-30", held_until=None, issued_by="test")))
    return path


def alter(path, **patch):
    value = json.loads(path.read_text())
    value.update(patch)
    path.write_text(json.dumps(value))


def test_admission_and_exact_reserve_boundary(window):
    assert sa.admit(window, "journey", 1200, NOW) == "ADMIT"
    assert sa.admit(window, "nightly-soak", 2400, "2026-11-15T10:40:00Z") == "ADMIT"
    assert sa.admit(window, "nightly-soak", 2400, "2026-11-15T10:40:01Z") == "BLOCKED:insufficient_remaining"


@pytest.mark.parametrize("patch,now,expected", [
    ({"host": "other-host"}, NOW, "invalid"),
    ({"valid_until": "2099-01-01"}, NOW, "invalid"),
    ({"valid_until": "2026-11-14"}, NOW, "expired"),
    ({"held_until": "2026-11-15T10:00:00Z"}, NOW, "held"),
    ({}, "2026-11-15T05:29:00Z", "outside_window"),
    ({}, "2026-11-15T11:30:00Z", "outside_window"),
    ({}, "2026-11-15T11:01:00Z", "insufficient_remaining"),
    ({"duration_s": True}, NOW, "invalid"),
    ({"held_until": "2026-11-15T10:00:00"}, NOW, "invalid"),
    ({"issued_by": ""}, NOW, "invalid"),
    ({"start_utc": "25:00"}, NOW, "invalid"),
])
def test_blocked_reasons(window, patch, now, expected):
    alter(window, **patch)
    assert sa.admit(window, "journey", 1200, now) == "BLOCKED:" + expected


def test_missing_and_malformed(window):
    assert sa.admit(window.with_name("missing"), "journey", 1200, NOW) == "BLOCKED:missing"
    window.write_text("{")
    assert sa.admit(window, "journey", 1200, NOW) == "BLOCKED:invalid"


def test_reject_iso_expiration_and_support_cross_midnight(window):
    alter(window, valid_until="2026-11-15T09:00:00Z")
    assert sa.admit(window, "journey", 1200, NOW) == "BLOCKED:invalid"
    alter(window, valid_until="2026-11-15T09:29:59Z")
    assert sa.admit(window, "journey", 1200, NOW) == "BLOCKED:invalid"
    alter(window, valid_until="2026-11-30", start_utc="23:00", duration_s=10800)
    assert sa.admit(window, "journey", 1200, "2026-11-16T00:00:00Z") == "ADMIT"


def test_cli_admit_never_executes_on_block(window, monkeypatch, capsys):
    monkeypatch.setattr(nj, "fire_guard", lambda: "BLOCKED:active_gate_or_deploy")
    assert nj.main(["admit", "--job", "nightly-soak", "--window", str(window), "--now-utc", NOW]) == 2
    assert capsys.readouterr().out.strip() == "BLOCKED:active_gate_or_deploy"


@pytest.mark.parametrize("script", ["gate_env.py", "run_gate.py", "deploy.py"])
def test_fire_guard_active_process(monkeypatch, script):
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=f"123 python python /fixture/{script} --run\n"))
    assert sa.fire_guard() == "BLOCKED:active_gate_or_deploy"


@pytest.mark.parametrize("args", ["python -m deploy.deploy --run", "python3.13 -u /fixture/gate_env.py --run", "/fixture/Python.app/Contents/MacOS/Python scripts/run_gate.py unit"])
def test_fire_guard_python_positions(monkeypatch, args):
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=f"123 python {args}\n"))
    assert sa.fire_guard() == "BLOCKED:active_gate_or_deploy"


@pytest.mark.parametrize("args", [
    "codex --prompt deploy gate_env.py run_gate.py",
    "claude --prompt deploy gate_env.py run_gate.py",
    "ssh fixture python /fixture/gate_env.py",
    "python /fixture/unrelated.py deploy gate_env.py",
    "python -c 'print(\"deploy\")' gate_env.py",
])
def test_fire_guard_ignores_prompt_data_and_remote_commands(monkeypatch, args):
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=f"123 program {args}\n"))
    assert sa.fire_guard() == "ADMIT"


def test_fire_guard_clear_and_unavailable(monkeypatch):
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="123 python /fixture/server.py\n"))
    assert sa.fire_guard() == "ADMIT"
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert sa.fire_guard() == "BLOCKED:process_table_unavailable"


def test_bound_kills_child_and_descendant(tmp_path):
    marker = tmp_path / "unwanted"
    descendant = f"import time; time.sleep(0.5); open({str(marker)!r},'w').write('escaped')"
    child = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{descendant!r}]); time.sleep(10)"
    receipt = tmp_path / "bound.json"
    assert sa.bounded([sys.executable, "-c", child], 0.15, receipt) == 2
    value = json.loads(receipt.read_text())
    assert value["terminated_at_bound"] is True and value["finished"] is True
    time.sleep(0.55)
    assert not marker.exists()


def test_bound_success(tmp_path):
    path = tmp_path / "bound.json"
    assert sa.bounded([sys.executable, "-c", "pass"], 2, path) == 0
    assert json.loads(path.read_text())["terminated_at_bound"] is False


def test_bound_contains_term_resistant_separate_sessions(tmp_path):
    # Leave room for real process inventory within the synthetic envelope.
    # Production's 1200/2400-second jobs give that snapshot up to five seconds.
    envelope_s = 3
    marker = tmp_path / "escaped"
    descendant = f"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep({envelope_s + 0.1}); open({str(marker)!r},'w').write('escaped')"
    child = f"import subprocess,sys,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); subprocess.Popen([sys.executable,'-c',{descendant!r}],start_new_session=True); time.sleep(10)"
    receipt = tmp_path / "bound.json"
    assert sa.bounded([sys.executable, "-c", child], envelope_s, receipt) == 2
    value = json.loads(receipt.read_text())
    assert value["terminated_at_bound"] and len(value["owned_at_stop"]) >= 2
    assert len(value["killed_owned_pids"]) >= 2
    assert not value.get("cleanup_errors")
    assert value["elapsed_s"] < envelope_s
    time.sleep(0.7)
    assert not marker.exists()


@pytest.fixture
def transports(tmp_path):
    """Each override is a real subprocess, with fake durable stores."""
    script = tmp_path / "transports.py"
    script.write_text('''import json,sqlite3,sys
from pathlib import Path
root=Path(sys.argv[1]); mode=sys.argv[2]; args=sys.argv[3:]
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps([mode,args])+'\\n')
if mode=='send':
    payload=json.load(sys.stdin)
    with (root/'rpc.jsonl').open('a') as out: out.write(json.dumps(payload)+'\\n')
    kind=payload['type']
    if kind=='assistant.binding': value={'type':'assistant.binding.ok','stream_id':'fixture:assistant','session_generation':'fixture-generation'}
    elif kind=='send.receipt.get': value={'type':'send.receipt.get.ok','receipts':[]}
    else: value={'type':'send.result','delivery':'committed_pending_proof' if (root/'pending').exists() else 'landed'}
elif mode=='publish':
    stem=args[0]+'__'+args[1]
    if any(p.name==stem for p in (root/'work').glob('*/*')):
        print(json.dumps({'ok':False,'code':'VALIDATION_FAILED','explanation':'item already exists'})); sys.exit(1)
    (root/'work'/'backlog'/stem).mkdir(parents=True)
    value={'ok':True,'saved':'fixture-commit'}
else:
    delivered=None if (root/'undelivered').exists() else '2026-11-15T09:00:00Z'
    with sqlite3.connect(root/'notices.db') as db: db.execute('INSERT INTO v2_outbound_notices VALUES(?,?,?)',('notice',json.dumps({'notification_id':'alert'}),delivered))
    value={'type':'notification.create.ok','notification':{'notification_id':'card'}}
print(json.dumps(value))
''')
    with sqlite3.connect(tmp_path / "notifications.db") as db:
        db.execute("CREATE TABLE notifications(notification_id TEXT,error_context TEXT)")
        db.execute("INSERT INTO notifications VALUES(?,?)", ("alert", json.dumps({"source_notification_id": "card"})))
    with sqlite3.connect(tmp_path / "notices.db") as db:
        db.execute("CREATE TABLE v2_outbound_notices(notice_id TEXT,metadata TEXT,delivered_at TEXT)")
    def run(result, date="2026-11-15", primary="", evidence=None):
        cmd = [sys.executable, str(Path(nj.__file__)), "report", "--job", "nightly-soak", "--result", result,
               "--date", date, "--primary-error", primary, "--evidence", str(evidence or tmp_path / date),
               "--notifications-db", str(tmp_path / "notifications.db"), "--notices-db", str(tmp_path / "notices.db"), "--notice-wait-s", "0"]
        for flag, mode in (("--report-send-cmd", "send"), ("--publisher-cmd", "publish"), ("--notify-cmd", "notify")):
            cmd += [flag, shlex.join([sys.executable, str(script), str(tmp_path), mode])]
        response = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        receipt = json.loads(((evidence or tmp_path / date) / "receipt.json").read_text())
        return response, receipt
    return run


def test_fake_soak_failure_report_critical_and_dedupe(transports, tmp_path):
    response, first = transports("FAIL", primary="socket timeout 123 /tmp/run-a/file")
    assert response.returncode == 0, response.stderr
    assert first["report_line"].startswith("REPORT nightly-soak date=2026-11-15 result=FAIL")
    assert first["report_delivery"]["landed"] is True
    assert first["notice_delivery"]["delivered_at"]
    assert first["item"].startswith("pentacle__nightly_soak_")
    stem = first["item"]
    (tmp_path / "work" / "completed").mkdir()
    (tmp_path / "work" / "backlog" / stem).rename(tmp_path / "work" / "completed" / stem)
    response, second = transports("FAIL", date="2026-11-16", primary="socket timeout 987 /tmp/run-b/file")
    assert response.returncode == 0, response.stderr
    assert second["item"] == stem and second["publisher"]["existing"]
    assert len(list((tmp_path / "work").glob("*/*"))) == 1
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert all("critical" in args for mode, args in calls if mode == "notify")


def test_fake_admission_blocked_reports_and_alerts(transports, tmp_path):
    response, receipt = transports("BLOCKED", primary="BLOCKED:held")
    assert response.returncode == 0, response.stderr
    assert receipt["item"] == "none" and receipt["notice_delivery"]["delivered_at"]
    assert receipt["report_delivery"]["landed"]
    assert not (tmp_path / "work").exists()


def test_pass_report_never_critical_or_published_and_retry_is_idempotent(transports, tmp_path):
    response, receipt = transports("PASS")
    assert response.returncode == 0 and receipt["report_delivery"]["landed"]
    response, _ = transports("PASS")
    calls = [json.loads(line)[0] for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert calls == ["send", "send", "send"]


def test_pending_proof_retries_once_without_claiming_delivery(transports, tmp_path):
    (tmp_path / "pending").touch()
    response, receipt = transports("PASS")
    assert response.returncode == 1
    assert receipt["report_delivery"]["landed"] is False
    sends = [json.loads(s) for s in (tmp_path / "rpc.jsonl").read_text().splitlines() if json.loads(s)["type"] == "send"]
    assert len(sends) == 2 and sends[0] == sends[1]
    assert "committed_pending_proof" in receipt["reporting_errors"][0]


def test_notification_insertion_is_not_delivery(transports, tmp_path):
    (tmp_path / "undelivered").touch()
    response, receipt = transports("BLOCKED", primary="BLOCKED:held")
    assert response.returncode == 1
    assert "notice_delivery" not in receipt
    assert "delivered_at" in receipt["reporting_errors"][0]


def test_signature_strips_volatile_values_but_keeps_row_and_class():
    a = nj.signature("nightly-soak", "full", "timeout 12 /tmp/a 2026-11-15T09:00:00Z id 12ab34cd-1234-5678-9012-abcdefabcdef")
    b = nj.signature("nightly-soak", "full", "timeout 44 /tmp/b 2026-11-16T09:00:00Z id abcdefab-5678-1234-9012-abcdefabcdef")
    assert a == b
    assert a != nj.signature("nightly-soak", "other", "timeout")
    assert a != nj.signature("nightly-soak", "full", "connection refused")


def test_both_seasonal_schedules_fit_and_old_times_fail(window):
    deploy = Path(nj.__file__).parents[1] / "deploy"
    config = plistlib.loads((deploy / "com.pentacle.nightly-soak.plist").read_bytes())
    assert config["StartCalendarInterval"] == {"Hour": 1, "Minute": 0}
    observed = []
    for hour, envelope in ((1, 2400), (3, 1200)):
        for month in (1, 7):
            local = datetime(2026, month, 15, hour, tzinfo=ZoneInfo("America/Chicago"))
            instant = local.astimezone(timezone.utc)
            alter(window, valid_until=instant.date().isoformat())
            assert sa.admit(window, "journey" if hour == 3 else "nightly-soak", envelope, instant) == "ADMIT"
            observed.append(instant.strftime("%H:%M"))
    assert observed == ["07:00", "06:00", "09:00", "08:00"]
    for hour, minute in ((9, 0), (0, 23)):
        instant = datetime(2026, 7, 15, hour, minute, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc)
        alter(window, valid_until=instant.date().isoformat())
        assert sa.admit(window, "nightly-soak", 2400, instant) == "BLOCKED:outside_window"
