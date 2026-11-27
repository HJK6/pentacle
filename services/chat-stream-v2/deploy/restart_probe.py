"""Send one tell per second across a daemon restart and record what happens to each.
  restart_probe.py run <target stream> <count> <out.jsonl>     (start ~5 s before the restart)
  restart_probe.py verify <out.jsonl>                           (read-only look at the daemon database)
Each probe has its own tell id. A transport failure is retried once with the same id (the documented
safe retry); a committed_pending_proof result is never resubmitted."""
import json, os, plistlib, sqlite3, subprocess, sys, time, uuid
from pathlib import Path

def tell(target, tell_id, text):
    t0 = time.time()
    p = subprocess.run(["agent-orch", "tell", "--tell-id", tell_id, "--timeout", "20", target, text],
                       capture_output=True, text=True)
    out = (p.stdout or "").strip().splitlines()
    try:
        reply = json.loads(out[-1]) if out else {}
    except ValueError:
        reply = {"raw": (p.stdout or "")[-300:]}
    return {"rc": p.returncode, "t0": t0, "dt": round(time.time() - t0, 3), "reply": reply, "stderr": (p.stderr or "")[-200:]}

def outcome(r):
    rep = r["reply"]; blob = json.dumps(rep)
    if "committed_pending_proof" in blob or rep.get("do_not_resubmit"): return "committed_pending_proof"
    if rep.get("type") == "tell.ok": return "ok:" + str(rep.get("delivery_status"))
    if "transport" in blob or "transmit" in blob or not rep: return "transport_failed"
    return "other:" + str(rep.get("error") or rep.get("type"))

def run(target, count, path):
    tag = uuid.uuid4().hex[:8]
    with open(path, "w") as f:
        for i in range(1, count + 1):
            tid = str(uuid.uuid4()); text = f"PROBE {tag}-{i:02d} of {count}: daemon restart delivery test. No reply and no action."
            a = tell(target, tid, text); rec = {"i": i, "tag": f"{tag}-{i:02d}", "tell_id": tid, "target": target, "first": a, "first_outcome": outcome(a)}
            if rec["first_outcome"] == "transport_failed":
                time.sleep(2); b = tell(target, tid, text); rec.update(retry=b, retry_outcome=outcome(b))
            f.write(json.dumps(rec) + "\n"); f.flush()
            print(i, rec["first_outcome"], rec.get("retry_outcome", ""), a["dt"], flush=True)
            time.sleep(max(0, 1 - a["dt"]))

def verify(path):
    plist = plistlib.loads((Path.home() / "Library/LaunchAgents" / (os.environ["PENTACLE_RELEASE_LAUNCHD_LABEL"] + ".plist")).read_bytes())
    argv = plist["ProgramArguments"]; db = argv[argv.index("--db") + 1]
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    bad = 0
    lines = Path(path).read_text().splitlines()
    if not lines: return 1
    for line in lines:
        r = json.loads(line); host, _, name = r["target"].partition(":")
        rows = conn.execute("SELECT count(*) FROM v2_tell_deliveries WHERE tell_id=?", (r["tell_id"],)).fetchone()[0]
        events = conn.execute("SELECT count(*) FROM session_event_tail WHERE stream_id=? AND event_json LIKE ?",
                              (r["target"], f"%PROBE {r['tag']} %")).fetchone()[0]
        ok = rows == 1 and events >= 1
        bad += not ok
        print(r["i"], r["first_outcome"], r.get("retry_outcome", "-"), f"delivery_rows={rows}", f"target_events={events}", "" if ok else "<-- CHECK")
    print("ALL DELIVERED ONCE" if not bad else f"{bad} probes need a look")
    return int(bool(bad))

if sys.argv[1] == "run": run(sys.argv[2], int(sys.argv[3]), sys.argv[4])
else: sys.exit(verify(sys.argv[2]))
