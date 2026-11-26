"""Writer round trips of one plain send through the real dispatch path.

Journey: a seat-token `send` of text to a local, open, account-bound Claude
seat, dispatched by `Server._dispatch` through real Comms, AccountRuntime,
CapacityRetry, lane rulings and Store; only the pane transport is fake. Every
`Store.submit` awaited by the dispatching task is counted, in order, so a
change in the number of sequential writer round trips fails here by name
(spec_pentacle__chat_writer_priority_2026_11 § A7).

At bf3a1139 (and with the release-three auth commits) the plain send made 41
and each extra physical-action guard 11 (10 for an Enter retry, which skips
the cap check). Joining the account input checks (slice 1) made that 30 and 3.
Slice 2 (R-014 addendum 1) joins consecutive submits with nothing awaited
between them, skips the QA target read without QA fields, and reuses one row
read while the lifecycle and pane locks are both held: 21 and 2.
"""
import asyncio
import hashlib
import json

import pytest

import comms as comms_mod
import store_call_diag
from capacity_retry import CapacityRetry
from comms import Comms
from server import Server
from spawnctl import SpawnCtl
from store import STREAM_TOKEN_HASH_VERSION
from provider_cli_fixtures import release_pair, seed_pin
from test_account_rotation import binding
from test_account_runtime import setup
from test_send_semantics import FakeBlobStore, FakeTmux, _submitted
from test_account_pool_control import OPERATOR

TOKEN = "synthetic-round-trip-token"
PEER = "host-a:peer"
PEER_GENERATION = "peer-one"
pytestmark = pytest.mark.timeout(60)


class LocalPeer:
    remote_address = ("127.0.0.1", 12345)


class DraftTmux(FakeTmux):
    """The paste stays in Claude's active draft until Enter submits it."""

    async def paste(self, _name, text):
        self.pastes.append(text)
        self.screen = f"❯ {text}\n"

    async def send_enter(self, _name):
        self.enters += 1
        self.screen = _submitted(self.pastes[-1])


async def world(tmp_path, tmux):
    store, runtime = await setup(initialize_pin=False)
    await seed_pin(runtime)
    account = binding(provider="claude")
    await runtime.run(lambda a: a.register(account))
    sessions = runtime.sessions
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    comms = Comms(store, sessions, ctl, blob_store=FakeBlobStore(), attachment_root=tmp_path / "attachments")
    server = Server(store=store, sessions=sessions, comms=comms)
    runtime.attach(server, ctl, comms)
    CapacityRetry(store, sessions, comms, None)
    worker = await sessions.open("host-a", "worker", provider="claude", session_generation="worker-one",
                                 account_binding=account.public(), pane_status="pane_alive")
    await sessions.open("host-a", "peer", provider="claude", session_generation=PEER_GENERATION)
    await store.grant_stream_token("host-a", "peer", hashlib.sha256(TOKEN.encode()).hexdigest(),
                                   STREAM_TOKEN_HASH_VERSION)
    pin = release_pair()["providers"]["claude"]["artifacts"]
    receipt = json.dumps({"artifact": {"platform": "darwin-arm64", "artifact": pin["darwin-arm64"]}})

    def admit(a):
        a.conn.execute("INSERT INTO kv VALUES (?,?)",
                       ("provider_cli.admission:" + worker["stream_id"] + ":worker-one", receipt))
        a.conn.commit()
    await runtime.run(admit)
    return store, server, worker


async def counted_send(store, server, worker, **extra):
    """Dispatch one send; return its result and the names of its own submits."""
    names: list[str] = []
    original = store.submit
    owner: list[asyncio.Task] = []

    async def submit(fn, **options):
        if owner and asyncio.current_task() is owner[0]:
            names.append(store_call_diag.call_name(fn))
        return await original(fn, **options)

    store.submit = submit
    msg = {"type": "send", "request_id": "round-trip-" + str(len(extra)), "stream_id": worker["stream_id"],
           "message": "plain owned work", "from_stream_id": PEER, "stream_token": TOKEN, **extra}

    async def dispatch():
        owner.append(asyncio.current_task())
        return (await server._dispatch(json.dumps(msg), websocket=LocalPeer()))[0]
    try:
        reply = await dispatch_task(dispatch)
    finally:
        store.submit = original
    return reply, names


async def dispatch_task(dispatch):
    return await asyncio.ensure_future(dispatch())


def run_send(tmp_path, tmux, **extra):
    async def go():
        store, server, worker = await world(tmp_path, tmux)
        try:
            return await counted_send(store, server, worker, **extra)
        finally:
            store.stop()
    return asyncio.run(go())


#: The account input guard around one physical action: one preparation read
#: and one locked recheck (its row is the one read under the send's locks).
GUARD = 2
PLAIN = 21


def test_plain_send_makes_the_counted_writer_round_trips(tmp_path):
    reply, names = run_send(tmp_path, FakeTmux())
    assert reply["type"] == "send.result" and reply["submission_confirmed"] is True, reply
    assert len(names) == PLAIN, "\n".join(names)


def test_urgent_send_adds_one_guard_for_the_escape(tmp_path):
    reply, names = run_send(tmp_path, FakeTmux(), urgent=True)
    assert reply["submission_confirmed"] is True, reply
    assert len(names) == PLAIN + GUARD, "\n".join(names)


def test_enter_retry_adds_one_proof_read_and_one_guard(tmp_path, monkeypatch):
    # One evidence pass per call: the draft fails the pane check once (one
    # event-tail read), Enter submits it and the second pass proves it.
    monkeypatch.setattr(comms_mod, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.0)
    tmux = DraftTmux()
    reply, names = run_send(tmp_path, tmux)
    assert reply["submission_confirmed"] is True and tmux.enters == 1, reply
    assert len(names) == PLAIN + 1 + GUARD, "\n".join(names)


#: An operator send through Comms (dispatch adds no actor reads for operators):
#: the guard keeps its direct-order cap write as its own settled submission.
OPERATOR_COMMS = 18


def test_operator_send_keeps_the_settled_cap_write_as_its_own_submission(tmp_path):
    async def go():
        tmux = FakeTmux()
        store, server, worker = await world(tmp_path, tmux)
        try:
            names: list[str] = []
            original = store.submit
            owner = []

            async def submit(fn, **options):
                if owner and asyncio.current_task() is owner[0]:
                    names.append((store_call_diag.call_name(fn), bool(options.get("settle_on_cancel"))))
                return await original(fn, **options)
            store.submit = submit

            async def send():
                owner.append(asyncio.current_task())
                return await server.comms.send({"stream_id": worker["stream_id"], "message": "operator work",
                                                "request_id": "operator-one", "_auth_context": OPERATOR})
            reply = await asyncio.ensure_future(send())
            store.submit = original
            assert reply["submission_confirmed"] is True, reply
            assert len(names) == OPERATOR_COMMS, "\n".join(map(str, names))
            # The paste guard: preparation, the locked recheck, then the settled claim.
            settled = [i for i, (_name, settle) in enumerate(names) if settle]
            assert len(settled) == 1 and names[settled[0] - 1][1] is False
            state = await store.submit(lambda c: c.execute(
                "SELECT v FROM kv WHERE k=?", ("account.cap-seat:" + worker["stream_id"] + ":worker-one",)).fetchone())
            assert json.loads(state[0])["operator_input"]["id"] == "operator-one"
        finally:
            store.stop()
    asyncio.run(go())


def test_cancel_while_the_locked_recheck_is_queued_records_no_operator_input(tmp_path):
    """A cancelled operator input never reaches its cap write: the locked
    recheck is an ordinary read, and the settled claim is submitted only after it."""
    import threading

    async def go():
        tmux = FakeTmux()
        store, server, worker = await world(tmp_path, tmux)
        runtime = server.account_runtime
        try:
            row = await store.fetch_session("host-a", "worker")
            entered, release = threading.Event(), threading.Event()
            inside = []
            await runtime.pin_lock.acquire()

            async def guarded():
                async with runtime.input_guard(row, message={"request_id": "cancelled", "_auth_context": OPERATOR},
                                               claim=True):
                    inside.append(True)
            task = asyncio.ensure_future(guarded())
            while not runtime.pin_lock._waiters:  # preparation done; waiting for the pin mutex
                await asyncio.sleep(0.005)
            held = asyncio.ensure_future(store.submit(lambda c: (entered.set(), release.wait(10))))
            assert await asyncio.to_thread(entered.wait, 5)
            runtime.pin_lock.release()
            while store.pending() < 1:  # the locked recheck is queued behind the held callback
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.05)
            release.set()
            await held
            with pytest.raises(asyncio.CancelledError):
                await task
            assert inside == [] and tmux.pastes == []
            state = await store.submit(lambda c: c.execute(
                "SELECT v FROM kv WHERE k=?", ("account.cap-seat:" + worker["stream_id"] + ":worker-one",)).fetchone())
            assert state is None or "operator_input" not in json.loads(state[0])
        finally:
            store.stop()
    asyncio.run(go())


def test_admitted_input_holds_the_home_reader_so_maintenance_waits(tmp_path, monkeypatch):
    """Maintenance begin's home writer arrives while an admitted input holds
    the reader, and waits behind it until that physical action ends."""
    from provider_accounts import Binding
    from test_loopback_privileged_rpc import rpc
    from test_provider_cli_maintenance import ready_daemon

    async def go():
        async with ready_daemon(tmp_path, monkeypatch, serving_seat=True) as (daemon, store, runtime, host, selected, row, request):
            home = runtime.home_inputs(Binding.from_public(row["account_binding"]))
            admitted, finish, arrived = asyncio.Event(), asyncio.Event(), asyncio.Event()
            readers_at_arrival = []
            write = home.write

            def arriving():  # the real home writer, observed as it arrives
                readers_at_arrival.append(home.readers)
                arrived.set()
                return write()
            home.write = arriving

            async def physical():
                async with runtime.input_guard(row):
                    admitted.set()
                    await finish.wait()
            first = asyncio.ensure_future(physical())
            await admitted.wait()
            freezing = asyncio.ensure_future(rpc(daemon, "spawn_freeze", **request))
            await arrived.wait()
            assert readers_at_arrival == [1], "maintenance arrived with no admitted input holding the home"
            while not home.waiting_writers:
                await asyncio.sleep(0.005)
            assert not home.writer and not freezing.done()
            finish.set()
            await first
            assert (await freezing)["type"] == "spawn_freeze.ok"
    asyncio.run(go())


def test_maintenance_begun_after_preparation_refuses_at_the_locked_recheck(tmp_path, monkeypatch):
    """Input paused after a successful preparation read, maintenance begins and
    completes, then the locked recheck refuses before the physical action."""
    from provider_accounts import AccountError
    from test_loopback_privileged_rpc import rpc
    from test_provider_cli_maintenance import ready_daemon

    async def go():
        async with ready_daemon(tmp_path, monkeypatch, serving_seat=True) as (daemon, store, runtime, host, selected, row, request):
            prepared, resume = asyncio.Event(), asyncio.Event()
            original = runtime.before_input

            async def paused(*args, **kwargs):
                await original(*args, **kwargs)
                prepared.set()
                await resume.wait()
            runtime.before_input = paused
            inside = []

            async def physical():
                async with runtime.input_guard(row):
                    inside.append(True)
            task = asyncio.ensure_future(physical())
            await prepared.wait()
            assert (await rpc(daemon, "spawn_freeze", **request))["type"] == "spawn_freeze.ok"
            resume.set()
            with pytest.raises(AccountError, match="provider_cli_input_maintenance"):
                await task
            assert inside == [] and not runtime.pin_lock.locked()
    asyncio.run(go())
