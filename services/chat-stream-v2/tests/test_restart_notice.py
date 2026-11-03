"""Front-desk restart notice (spec_pentacle__fd_restart_resume_notice_2026_11).

Pins the plan's step-2 cells: (a) classification, (b) composed sections, caps
and the byte limit, (c) real digest admission, (d) marker + per-process dedup,
(e) compose fault -> header only, (f) a stalled probe never delays the hook,
(g) durable queue + rebind flush with the notice sender, (h) daemon-wide
deduplicated questions. The real-process cells are tests/soak/test_restart_notice.py.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import main as daemon_main
import restart_notice as rn
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from assistant_lane_rulings import RULING_REQUESTS_DDL
from front_desk_digest import FrontDeskDigest
from notify import Notify
from server import Server
from sessions import Sessions, VerbError
from store import Store

LOCAL = "hosta"
FD = "hosta:v2-fd"
ADVISOR = "hosta:v2-adv"
ASSISTANT_CHAT = "home:assistant"
NOW = 1_793_700_000.0
HOLDS_DDL = ("CREATE TABLE IF NOT EXISTS holds (id TEXT PRIMARY KEY, resource TEXT NOT NULL, "
             "owner_stream TEXT NOT NULL, reason TEXT NOT NULL, acquired_at TEXT NOT NULL, "
             "expires_at TEXT, released_at TEXT, release_cause TEXT)")


def _config(generation: str) -> AssistantCompositeConfig:
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT_CHAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": FD,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
    })


async def _seat(store, stream_id, **fields):
    host, name = stream_id.split(":", 1)
    fields.setdefault("role", "lead")
    return await store.open_session(host, name, provider="claude", visibility="hidden",
                                    pane_status="pane_alive", **fields)


async def _composite(store, fd_row):
    composite = AssistantComposite(store, config=_config(fd_row["session_generation"]))
    await composite.load_binding()
    await composite.ensure_projection()
    return composite


def _probe(states, *, stall=()):
    async def probe(host, name):
        stream = f"{host}:{name}"
        if stream in stall:
            await asyncio.sleep(3600)
        if host != LOCAL:
            raise VerbError("ssh_unreachable", "peer unreachable")
        return states.get(stream, "gone")
    return probe


def _queue_into(store):
    """Stand-in for the closure with an unbound front desk: durable enqueue."""
    async def deliver(composite, text, tell_id, *, from_stream_id):
        await store.enqueue_composite_tell(name=composite.config.name, tell_id=tell_id,
                                           from_stream_id=from_stream_id, body=text, request_id="")
        return "queued"
    return deliver


def _notice(store, composite, *, probe, notify=None, env=None, deliver=None, **kw):
    return rn.RestartNotice(
        store=store, composite=composite, deliver=deliver or _queue_into(store), probe=probe, notify=notify,
        instance_id=kw.pop("instance_id", "hosta:4242:abc"), daemon_sha=kw.pop("daemon_sha", "b" * 40),
        pid=4242, started_at=kw.pop("started_at", NOW), env=env or {},
        boot_time=kw.pop("boot_time", lambda: 1_793_690_000), clock=kw.pop("clock", lambda: NOW), **kw)


async def _queued(store, tell_id):
    return await store.submit(lambda conn: [dict(r) for r in conn.execute(
        "SELECT * FROM v2_assistant_composite_tell_queue WHERE tell_id=?", (tell_id,))])


# --------------------------------------------------------------------------- #
# (a) classification table
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("previous,current,kind", [
    (None, {"boot_time": 100, "daemon_sha": "a"}, "first_instrumented_start"),
    ({}, {"boot_time": 100, "daemon_sha": "a"}, "first_instrumented_start"),
    ({"boot_time": 100, "daemon_sha": "a"}, {"boot_time": 900, "daemon_sha": "b"}, "host_boot"),
    ({"boot_time": 100, "daemon_sha": "a"}, {"boot_time": 900, "daemon_sha": "a"}, "host_boot"),
    ({"boot_time": 100, "daemon_sha": "a"}, {"boot_time": 100, "daemon_sha": "b"}, "deploy"),
    ({"boot_time": 100, "daemon_sha": "a"}, {"boot_time": 101, "daemon_sha": "b"}, "deploy"),
    ({"boot_time": 100, "daemon_sha": "a"}, {"boot_time": 100, "daemon_sha": "a"}, "daemon_restart"),
    ({"boot_time": 100, "daemon_sha": "a"}, {"boot_time": None, "daemon_sha": ""}, "daemon_restart"),
])
def test_classification_table(previous, current, kind):
    assert rn.classify(previous, current) == kind


@pytest.mark.parametrize("value,expected", [(None, True), ("1", True), (" 0 ", False)])
def test_flag_parsing(value, expected):
    env = {} if value is None else {"PENTACLE_RESTART_NOTICE": value}
    assert rn.parse_flag(env) is expected


@pytest.mark.parametrize("value", ["", "2", "true", "off"])
def test_invalid_flag_fails_startup(value):
    with pytest.raises(ValueError, match="PENTACLE_RESTART_NOTICE"):
        rn.parse_flag({"PENTACLE_RESTART_NOTICE": value})


def test_disabled_flag_skips_compose_delivery_and_marker_in_main():
    """The kill switch gates the whole hook in main.run (source pin)."""
    import inspect
    src = inspect.getsource(daemon_main.run)
    assert "restart_notice_enabled = restart_notice.parse_flag(os.environ)" in src
    assert "if restart_notice_enabled and assistant_composite.config.enabled:" in src
    hook = src.index("restart_notice.RestartNotice(")
    assert src.index("await server.configure_error_alerts(outbound, alerts)") < hook
    assert hook < src.index('tasks: list[asyncio.Task] = [')


# --------------------------------------------------------------------------- #
# (b) compose against a seeded store
# --------------------------------------------------------------------------- #

async def _seed(store, tmp_path, *, checkpoint=True, notify=None):
    fd = await _seat(store, FD, role="assistant")
    await _seat(store, ADVISOR, role="lead")
    await _seat(store, "hosta:v2-alive", parent_stream_id=FD, spec_id="spec_x__alive", title="Alive child")
    await _seat(store, "hosta:v2-gone", parent_stream_id=FD, spec_id="spec_x__gone", title="Gone child")
    await _seat(store, "peer:v2-remote", parent_stream_id="hosta:v2-nexus", title="Remote child")
    await _seat(store, "hosta:v2-print", spec_id="spec_printer__drawer_2026_10", title='Print "lead"')
    await store.put("assistant.authority.shape", json.dumps({"stream_id": ADVISOR, "generation": "g"}))

    def seed(conn):
        conn.execute("INSERT INTO v2_spawn_admission_hold(host, held_until, reason, set_at) VALUES(?,?,?,?)",
                     ("hosta", NOW + 600, "deploy freeze", NOW))
        conn.execute("INSERT INTO v2_spawn_admission_hold(host, held_until, reason, set_at) VALUES(?,?,?,?)",
                     ("old", NOW - 600, "expired", NOW - 900))
        conn.execute(HOLDS_DDL)
        conn.execute("INSERT INTO holds VALUES('h1','hosta:gate','hosta:v2-alive','merge gate',?,?,NULL,NULL)",
                     ("2026-11-03T00:00:00Z", "2099-01-01T00:00:00Z"))
        conn.execute("INSERT INTO holds VALUES('h2','hosta:old','hosta:v2-alive','old',?,?,NULL,NULL)",
                     ("2026-11-01T00:00:00Z", "2026-11-01T01:00:00Z"))
        conn.execute("INSERT INTO v2_screen_leases(lease_id, host, holder_stream_id, holder_label, reason, "
                     "acquired_at, expires_at) VALUES('l1','hosta','hosta:v2-alive','lead','ui walk',?,?)",
                     (NOW - 60, NOW + 300))
        conn.execute(RULING_REQUESTS_DDL)
        base = ("intent", "{}", "g", ADVISOR, "g", NOW - 120, NOW + 600)
        conn.execute(
            "INSERT INTO v2_assistant_lane_rulings(ruling_request_id, request_key, action, intent_digest, "
            "intent_json, requester_generation, authority_stream_id, authority_generation, created_at, deadline, "
            "requester_stream_id, target_stream_id, state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ruling-1", "k1", "spawn", *base, "hosta:v2-alive", "hosta:v2-new", "pending"))
        conn.execute(
            "INSERT INTO v2_assistant_lane_rulings(ruling_request_id, request_key, action, intent_digest, "
            "intent_json, requester_generation, authority_stream_id, authority_generation, created_at, deadline, "
            "requester_stream_id, target_stream_id, state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ruling-2", "k2", "close", *base, "hosta:v2-alive", None, "release_blocked"))
        held = {"envelope": {"question_id": "q-held", "producer_stream_id": "hosta:v2-gone",
                             "spec_id": "spec_x__gone", "created_at": "2026-11-03T00:00:00Z"}}
        dup = {"envelope": {"question_id": "q-store", "producer_stream_id": FD,
                            "spec_id": "spec_fd", "created_at": "2026-11-03T00:00:00Z"}}
        for rid, intent, state in (("ruling-q1", held, "pending"), ("ruling-q2", dup, "approved")):
            conn.execute(
                "INSERT INTO v2_assistant_lane_rulings(ruling_request_id, request_key, action, intent_digest, "
                "intent_json, requester_stream_id, requester_generation, authority_stream_id, "
                "authority_generation, created_at, deadline, state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, rid, "question", "d", json.dumps(intent), intent["envelope"]["producer_stream_id"],
                 "g", ADVISOR, "g", NOW - 30, NOW + 600, state))
        conn.commit()

    await store.submit(seed)
    await store.enqueue_outbound_notice(
        notice_id="n1", tell_id="n1", kind="front_desk_held", dedupe_key="n1",
        recipient_stream_id=FD, source_stream_id="hosta:v2-alive", body="held", metadata={})
    if notify is not None:
        await notify._db.call("create_agent_question", envelope={
            "schema_version": 1, "question_id": "q-store", "title": "Approve?", "body": "Approve?",
            "context": None, "created_at": "2026-11-02T23:00:00Z", "updated_at": "2026-11-02T23:00:00Z",
            "dedup_key": f"agent-question:{FD}:q-store", "producer_stream_id": FD, "producer_provider": None,
            "spec_id": "spec_fd", "response_mode": "free_text", "options": [], "ttl_seconds": None,
        }, actions=[])
    path = tmp_path / "checkpoint-current.md"
    if checkpoint:
        path.write_text("x" * 1234)
    return fd, {"PENTACLE_FD_CHECKPOINT_PATH": str(path), "PENTACLE_ASSISTANT_AUTO_RESTORE": "1"}


def _run(body):
    async def wrapper():
        store = Store(":memory:")
        store.start()
        try:
            await body(store)
        finally:
            store.stop()
    asyncio.run(wrapper())


SECTION_PREFIXES = ["BLOCKER restart from daemon: ", "Front desk: ", "Advisor: ", "Seats open at start: ",
                    "Print work: ", "Windows: ", "Advisor rulings: ", "Operator questions: ",
                    "Undelivered FD traffic: ", "Checkpoint: ", "Resume: "]


def _section_order(text):
    lines = text.splitlines()
    return [next(i for i, l in enumerate(lines) if l.startswith(p)) for p in SECTION_PREFIXES]


@pytest.mark.parametrize("checkpoint", [True, False])
def test_compose_lists_every_section_in_order(tmp_path, checkpoint):
    async def body(store):
        notify = Notify(str(tmp_path / "notifications.db"))
        await notify.start()
        try:
            fd, env = await _seed(store, tmp_path, checkpoint=checkpoint, notify=notify)
            composite = await _composite(store, fd)
            states = {"hosta:v2-alive": "alive", "hosta:v2-print": "alive"}  # FD/advisor/gone: gone
            notice = _notice(store, composite, probe=_probe(states), notify=notify, env=env)
            marker = await notice.run_once(await notice.capture())
            text = (await _queued(store, notice.tell_id))[0]["body"]
        finally:
            await notify.stop()
        assert marker["kind"] == "first_instrumented_start"
        order = _section_order(text)
        assert order == sorted(order) and len(set(order)) == 11
        lines = text.splitlines()
        assert lines[0].startswith("BLOCKER restart from daemon: first_instrumented_start — host booted ")
        assert lines[0].endswith("; no previous marker")
        assert f"Front desk: {FD} generation {fd['session_generation']} pane gone; auto-restore episode expected" in text
        assert f"Advisor: {ADVISOR} pane gone" in text
        assert "Seats open at start: 4 open: 2 alive, 1 gone, 1 unknown (probed 2026-" in text
        seat_lines = [l for l in lines[order[3] + 1:order[4]] if l.startswith("  hosta:") or l.startswith("  peer:")]
        assert [l.split()[0] for l in seat_lines] == ["hosta:v2-gone", "peer:v2-remote", "hosta:v2-alive", "hosta:v2-print"]
        assert seat_lines[0] == '  hosta:v2-gone lead spec_x__gone "Gone child" gone parent=FD'
        assert seat_lines[1].endswith("unknown parent=hosta:v2-nexus")
        assert "\"Print 'lead'\" alive parent=top-level" in seat_lines[3]
        assert rn.SEATS_FOOTER in text and "(provisional)" in text
        assert FD not in [l.split()[0] for l in seat_lines] and ADVISOR not in [l.split()[0] for l in seat_lines]
        print_block = "\n".join(lines[order[4]:order[5]])
        assert "Print work: 1 seat(s)" in print_block and "hosta:v2-print" in print_block
        assert ("printer state is not probed by the daemon; check: "
                "printer status, as given in docs/abilities/printer_quest.md") in print_block
        windows = "\n".join(lines[order[5]:order[6]])
        assert "Windows: 3 active" in windows
        assert 'admission freeze hosta until ' in windows and "old" not in windows.replace("hold", "")
        assert "hold hosta:gate owner hosta:v2-alive" in windows and "hosta:old" not in windows
        assert "screen lease hosta holder hosta:v2-alive" in windows
        rulings = "\n".join(lines[order[6]:order[7]])
        assert "Advisor rulings: 2 pending" in rulings  # the held question ruling is pending too
        assert "ruling-1 spawn requester=hosta:v2-alive target=hosta:v2-new age 120s" in rulings
        assert "other non-done: approved 1, release_blocked 1" in rulings
        undelivered = "\n".join(lines[order[8]:order[9]])
        assert "Undelivered FD traffic: 1 notice(s); composite queue depth 0 before this notice" in undelivered
        assert "by kind: front_desk_held 1" in undelivered
        if checkpoint:
            assert lines[order[9]].endswith("exists 1234 bytes modified " + lines[order[9]].split("modified ")[1])
            assert "x" * 10 not in text  # path only, never the contents
        else:
            assert lines[order[9]].endswith("checkpoint-current.md missing")
        assert lines[-1] == rn.RESUME_LINE
        assert len(text.encode()) <= rn.CAP_BYTES
    _run(body)


def test_empty_sections_say_none(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        composite = await _composite(store, fd)
        notice = _notice(store, composite, probe=_probe({FD: "alive"}),
                         env={"PENTACLE_FD_CHECKPOINT_PATH": str(tmp_path / "nope.md")})
        await notice.run_once(await notice.capture())
        text = (await _queued(store, notice.tell_id))[0]["body"]
        for line in ("Advisor: none configured", "Seats open at start: none", "Print work: none",
                     "Windows: none", "Advisor rulings: none", "Operator questions: none"):
            assert line in text.splitlines(), line
        assert f"pane alive; no restore needed" in text
        assert "Undelivered FD traffic: 0 notice(s)" in text
    _run(body)


def _facts(n_seats=0, n_prints=0, n_questions=0, n_rulings=0, n_windows=0, title="t" * 40, pad=30):
    seat = lambda i, spec: {"stream_id": f"hosta:v2-{i:04d}" + "x" * pad, "role": "worker", "spec_id": spec,
                            "title": title, "probe": "gone", "created_at": f"2026-11-03T00:{i % 60:02d}:00Z",
                            "parent_stream_id": "hosta:v2-parent-with-a-long-name"}
    seats = [seat(i, "spec_x" + "_" * pad) for i in range(n_seats)]
    seats += [seat(1000 + i, "spec_printer__job" + "_" * pad) for i in range(n_prints)]
    return {
        "kind": "daemon_restart", "header": "BLOCKER restart from daemon: daemon_restart — header",
        "front_desk": {"stream_id": FD, "generation": "g", "probe": "alive"},
        "advisor": {"stream_id": "", "probe": "unknown"}, "seats": seats, "probed_at": NOW,
        "printer_cmd": rn.DEFAULT_PRINTER_CMD,
        "windows": {"admission": [], "holds": [{"resource": f"r{i}" * 20, "owner_stream": "o" * 40,
                                                "expires_at": None} for i in range(n_windows)], "screen": []},
        "rulings": {"pending": [{"ruling_request_id": f"ruling-{i}" + "r" * 40, "action": "spawn",
                                 "requester_stream_id": "q" * 30, "target_stream_id": None,
                                 "created_at": NOW - 5} for i in range(n_rulings)], "other_counts": {}},
        "questions": [{"question_id": f"q{i}" + "z" * 40, "producer": "p" * 30, "spec_id": "s" * 40,
                       "created_at": "2026-11-03T00:00:00Z", "source": "store"} for i in range(n_questions)],
        "undelivered": {"by_kind": {"front_desk_held": 3}, "queue_depth": 0},
        "checkpoint": {"path": "/x/checkpoint-current.md", "exists": False},
    }


def test_per_list_caps_and_overflow_commands():
    facts = _facts(n_seats=25, n_prints=10, n_questions=9, n_rulings=9, n_windows=9, title="t", pad=0)
    for seat in facts["seats"]:
        seat["parent_stream_id"] = FD
    for q in facts["questions"]:
        q.update(question_id=q["question_id"][:2], producer="p", spec_id="s")
    for r in facts["rulings"]["pending"]:
        r.update(ruling_request_id=r["ruling_request_id"][:8], requester_stream_id="q")
    for h in facts["windows"]["holds"]:
        h.update(resource="r", owner_stream="o")
    text = rn.compose(facts)
    assert "omitted for size" not in text and len(text.encode()) <= rn.CAP_BYTES
    assert "  +15 more: agent-orch list" in text  # 35 seats, 20 shown
    assert "  +2 more: agent-orch list" in text  # 10 print seats, 8 shown (full population)
    assert "  +1 more: agent-orch prompt list --open" in text
    assert "  +1 more pending" in text
    assert "  +1 more hold: agent-orch hold list" in text
    assert "Print work: 10 seat(s)" in text  # print seats beyond the 20 displayed still count


def test_byte_cap_drops_lists_in_priority_order_and_keeps_fixed_lines():
    facts = _facts(n_seats=40, n_prints=10, n_questions=9, n_rulings=9, n_windows=9)
    text = rn.compose(facts)
    assert len(text.encode()) <= rn.CAP_BYTES
    assert text.splitlines()[0] == facts["header"]
    assert "Front desk: " in text and "Checkpoint: " in text and text.splitlines()[-1] == rn.RESUME_LINE
    assert "(50 listed in the count above, omitted for size: agent-orch list)" in text  # seats first
    assert "Seats open at start: 50 open:" in text  # its count line survives
    # Questions are next in priority; with seats dropped this fits, so they stay.
    assert "Operator questions: 9 open" in text and "q0" in text


def test_byte_cap_keeps_dropping_after_seats():
    facts = _facts(n_seats=40, n_prints=10, n_questions=9, n_rulings=9, n_windows=9, title="t" * 40)
    for q in facts["questions"]:
        q["producer"] = "p" * 290
    for r in facts["rulings"]["pending"]:
        r["requester_stream_id"] = "q" * 290
    text = rn.compose(facts)
    assert len(text.encode()) <= rn.CAP_BYTES
    assert "(9 listed in the count above, omitted for size: agent-orch prompt list --open)" in text
    assert "Advisor rulings: 9 pending" in text


# --------------------------------------------------------------------------- #
# (c) the real front-desk digest admits the notice and still holds the restore text
# --------------------------------------------------------------------------- #

def test_digest_admits_the_notice_as_a_wake_and_still_holds_seat_restored(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        config = _config(fd["session_generation"])
        digest = FrontDeskDigest(store, lambda: config)
        text = rn.compose(_facts(n_seats=2))
        msg = {"from_stream_id": rn.FROM_STREAM_ID, "tell_id": "restart-notice:x"}
        assert await digest.ingress(target_stream_id=FD, body=text, msg=msg, verb="tell") is None
        restored = "Seat restored after a host restart; binding revision 15. The lifecycle-manager grant is not restored."
        held = await digest.ingress(target_stream_id=FD, body=restored,
                                    msg={"from_stream_id": "daemon:assistant-restore", "tell_id": "r1"}, verb="tell")
        assert held is not None and held.get("front_desk_hold_id")
    _run(body)


# --------------------------------------------------------------------------- #
# (d) marker + per-process dedup; (e) compose fault
# --------------------------------------------------------------------------- #

def test_marker_written_once_and_second_run_is_a_noop(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        composite = await _composite(store, fd)
        notice = _notice(store, composite, probe=_probe({FD: "alive"}))
        snapshot = await notice.capture()
        marker = await notice.run_once(snapshot)
        stored = json.loads(await store.get(rn.MARKER_KEY))
        assert stored == marker
        assert set(stored) == {"instance_id", "pid", "started_at", "boot_time", "daemon_sha", "notice_tell_id", "kind"}
        assert stored["notice_tell_id"] == "restart-notice:hosta:4242:abc"
        assert await notice.run_once(snapshot) is None
        # A second object in the same process (same instance_id) also no-ops.
        again = _notice(store, composite, probe=_probe({FD: "alive"}))
        assert await again.run_once(snapshot) is None
        assert len(await _queued(store, notice.tell_id)) == 1
        # The next process (new instance, same boot + sha) is a daemon_restart.
        later = _notice(store, composite, probe=_probe({FD: "alive"}), instance_id="hosta:5555:def",
                        started_at=NOW + 90)
        nxt = await later.run_once(await later.capture())
        assert nxt["kind"] == "daemon_restart"
        body_text = (await _queued(store, later.tell_id))[0]["body"]
        assert body_text.splitlines()[0].endswith(f"previous daemon started {rn._utc(NOW)} (90 s before this start) sha bbbbbbb")
        deploy = _notice(store, composite, probe=_probe({FD: "alive"}), instance_id="hosta:6:x", daemon_sha="c" * 40)
        assert (await deploy.run_once(await deploy.capture()))["kind"] == "deploy"
    _run(body)


def test_compose_failure_sends_the_header_and_still_writes_the_marker(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        composite = await _composite(store, fd)

        def boom(facts):
            raise RuntimeError("compose fault")

        notice = _notice(store, composite, probe=_probe({FD: "alive"}), compose_fn=boom)
        marker = await notice.run_once(await notice.capture())
        text = (await _queued(store, notice.tell_id))[0]["body"]
        assert text.startswith("BLOCKER restart from daemon: first_instrumented_start — host booted ")
        assert "\n" not in text
        assert "error" not in marker and json.loads(await store.get(rn.MARKER_KEY))["kind"] == marker["kind"]
    _run(body)


def test_delivery_failure_records_error_on_the_marker(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        composite = await _composite(store, fd)

        async def failing(*a, **k):
            raise OSError("store gone")

        notice = _notice(store, composite, probe=_probe({FD: "alive"}), deliver=failing)
        marker = await notice.run_once(await notice.capture())
        assert marker["error"].startswith("OSError")
        assert json.loads(await store.get(rn.MARKER_KEY))["notice_tell_id"] == notice.tell_id
    _run(body)


# --------------------------------------------------------------------------- #
# (f) a stalled probe never delays the hook
# --------------------------------------------------------------------------- #

def test_stalled_probe_does_not_delay_the_hook_and_reads_unknown(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        await _seat(store, "hosta:v2-stuck", parent_stream_id=FD)
        composite = await _composite(store, fd)
        notice = _notice(store, composite, probe=_probe({FD: "alive"}, stall={"hosta:v2-stuck"}),
                         clock=time.time)
        started = time.monotonic()
        task = notice.schedule(await notice.capture())
        assert time.monotonic() - started < 0.5 and not task.done()
        await asyncio.sleep(0)
        assert await _queued(store, notice.tell_id) == []  # still probing in the background
        marker = await asyncio.wait_for(task, 15)
        elapsed = time.monotonic() - started
        assert rn.PROBE_TIMEOUT_S <= elapsed < rn.PROBE_TIMEOUT_S + 5
        text = (await _queued(store, notice.tell_id))[0]["body"]
        assert '"" unknown parent=FD' in text and marker["kind"] == "first_instrumented_start"
    _run(body)


# --------------------------------------------------------------------------- #
# (g) durable queue through the real delivery closure, then a rebind flush
# --------------------------------------------------------------------------- #

class _DeadPaneComms:
    """The bound pane is gone: the paste fails the way a dead tmux target does."""

    def __init__(self):
        self.told = []

    async def tell(self, msg):
        raise VerbError("tmux_failed", "can't find session")


class _LiveComms:
    def __init__(self, status="delivered"):
        self.told, self.status = [], status

    async def tell(self, msg):
        self.told.append(dict(msg))
        return {"type": "tell.ok", "delivery_status": self.status, "submission_confirmed": False}


def _server(store, comms, composite):
    server = Server(store=store, sessions=Sessions(store, tmux=None, local_host=LOCAL), comms=comms, local_host=LOCAL)
    server.assistant_composites = {"assistant": composite}
    server.assistant_composite = composite
    return server


def _closure(server, store):
    async def deliver(composite, text, tell_id, *, from_stream_id):
        return await daemon_main._deliver_composite_notice(
            server, store, composite, text, tell_id, from_stream_id=from_stream_id)
    return deliver


def test_dead_pane_open_row_queues_and_rebind_flush_delivers_with_notice_sender(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")  # open row, no presumed_dead_at
        composite = await _composite(store, fd)
        server = _server(store, _DeadPaneComms(), composite)
        notice = _notice(store, composite, probe=_probe({}), deliver=_closure(server, store))
        marker = await notice.run_once(await notice.capture())
        assert "error" not in marker
        [row] = await _queued(store, notice.tell_id)
        assert row["from_stream_id"] == rn.FROM_STREAM_ID
        assert row["body"].startswith("BLOCKER restart from daemon: ") and f"{FD} generation" in row["body"]

        resumed = await _seat(store, FD, role="assistant")  # rebind: same stream, new generation
        composite2 = AssistantComposite(store, config=_config(resumed["session_generation"]))
        await composite2.load_binding()
        server.comms = _LiveComms()
        assert await server._flush_composite_tells(composite2) == 1
        [told] = server.comms.told
        assert told["from_stream_id"] == "daemon:restart-notice" and told["tell_id"] == notice.tell_id
        assert await _queued(store, notice.tell_id) == []
    _run(body)


def test_uncommitted_handoff_is_not_receipt_and_stays_queued(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        composite = await _composite(store, fd)
        server = _server(store, _LiveComms(status="pasted_unsubmitted"), composite)
        notice = _notice(store, composite, probe=_probe({FD: "alive"}), deliver=_closure(server, store))
        await notice.run_once(await notice.capture())
        assert len(server.comms.told) == 1
        assert [r["from_stream_id"] for r in await _queued(store, notice.tell_id)] == [rn.FROM_STREAM_ID]
    _run(body)


def test_committed_delivery_is_not_queued(tmp_path):
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        composite = await _composite(store, fd)
        server = _server(store, _LiveComms(), composite)
        notice = _notice(store, composite, probe=_probe({FD: "alive"}), deliver=_closure(server, store))
        await notice.run_once(await notice.capture())
        assert server.comms.told[0]["from_stream_id"] == rn.FROM_STREAM_ID
        assert await _queued(store, notice.tell_id) == []
    _run(body)


def test_restore_notice_closure_keeps_its_sender_and_queue_rule(tmp_path):
    """The restore caller is unchanged: default sender, queue only when unbound."""
    import assistant_restore
    async def body(store):
        fd = await _seat(store, FD, role="assistant")
        composite = await _composite(store, fd)
        await store.mark_reconciled_dead(LOCAL, "v2-fd", expected_generation=fd["session_generation"],
                                         presumed_dead_at="2026-11-03T00:00:00Z", closed_at="2026-11-03T00:01:00Z")
        server = _server(store, _LiveComms(), composite)
        outcome = await daemon_main._deliver_composite_notice(
            server, store, composite, "Seat restored…", "restore-1", from_stream_id=assistant_restore.NOTICE_FROM)
        assert outcome == "queued" and server.comms.told == []
        [row] = await _queued(store, "restore-1")
        assert row["from_stream_id"] == assistant_restore.NOTICE_FROM
    _run(body)


# --------------------------------------------------------------------------- #
# (h) daemon-wide questions, deduplicated by question_id
# --------------------------------------------------------------------------- #

def test_questions_are_daemon_wide_and_deduplicated(tmp_path):
    async def body(store):
        notify = Notify(str(tmp_path / "notifications.db"))
        await notify.start()
        try:
            fd, env = await _seed(store, tmp_path, notify=notify)
            composite = await _composite(store, fd)
            notice = _notice(store, composite, probe=_probe({}), notify=notify, env=env)
            await notice.run_once(await notice.capture())
            text = (await _queued(store, notice.tell_id))[0]["body"]
            stored = await notify._db.call("get_agent_question", "q-store")
        finally:
            await notify.stop()
        lines = text.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("Operator questions: "))
        block = lines[start:start + 3]
        assert block[0] == "Operator questions: 2 open"
        assert block[1].startswith(f"  q-store {FD} spec_fd age ") and block[1].endswith(" store")
        assert block[2].startswith("  q-held hosta:v2-gone spec_x__gone age ") and block[2].endswith(" held")
        assert text.count("q-store") == 1
        assert stored["state"] == "open"  # read-only: no question state changed
    _run(body)
