"""Operator-only household adapter (Pentacle Personal -> Cosmo); fake Cosmo, no live services.

Spec: spec_pentacle_mobile__personal_screens_2026_10 § B2 / Validation V2.
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import pytest

from _shared import operator_auth
from household import Household

TOKEN = "synthetic-pentacle-token-value"
#: Cosmo person value of the operator in these fixtures (configured by PENTACLE_COSMO_SELF in a deployment).
SELF = "owner"
OPERATOR = {"operator_authenticated": True, "operator_principal": "operator:cid", "transport": "v2"}
# 2026-10-07 03:00 UTC is 2026-10-06 22:00 in America/Chicago.
NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc).timestamp()


class _FakeCosmoServer(ThreadingHTTPServer):
    # household.snapshot opens one connection per list plus two event windows at once; macOS
    # refuses connects beyond socketserver's default listen backlog of 5 (ECONNRESET/ECONNREFUSED),
    # which the adapter correctly reports as "household store unavailable".
    request_queue_size = 64


class FakeCosmo:
    """Records every request; replies from a (method, path) table."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.delay = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                split = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.calls.append({"method": self.command, "path": split.path,
                                   "query": parse_qs(split.query), "body": body,
                                   "auth": self.headers.get("Authorization")})
                key = (self.command, split.path)
                time.sleep(fake.delay.get(key, 0))
                status, payload = fake.routes.get(key, (404, {"detail": "not found"}))
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if status != 204:
                    self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = do_DELETE = _serve

        self.httpd = _FakeCosmoServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def item(ident, list_name="tasks", done_at=None, **extra):
    return {"id": ident, "list": list_name, "label": f"item {ident}", "done_at": done_at, "priority": "med",
            "due_date": None, "position": ident, "category": None, "scope": "owner", "created_by": "app",
            "routine_id": None, **extra}


def event(ident, date, **extra):
    return {"id": ident, "date": date, "time": None, "title": f"event {ident}", "who": "owner",
            "location": None, "star": False, "scope": "owner", "created_by": "app", **extra}


@pytest.fixture
def cosmo():
    fake = FakeCosmo()
    for name in ("tasks", "grocery", "meals", "chores", "study"):
        fake.routes[("GET", f"/lists/{name}/items")] = (200, {"items": [], "server_now": "s"})
    fake.routes[("GET", "/events")] = (200, {"items": [], "server_now": "s"})
    yield fake
    fake.close()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "pentacle.token"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o600)
    yield path
    if path.exists():
        path.unlink()


@pytest.fixture
def adapter(cosmo, token_file):
    return Household(url=cosmo.url, token_file=str(token_file), clock=lambda: NOW, allow_insecure=True,
                     self_person=SELF, partner_name="Sam")


def call(adapter, verb, **fields):
    msg = {"type": verb, "request_id": "r1", "_auth_context": OPERATOR, **fields}
    return asyncio.run(adapter.wire_handlers()[verb](msg))


def code_of(excinfo):
    return excinfo.value.code


VERBS = {
    "household.snapshot": {},
    "household.item.add": {"list": "grocery", "label": "Limes"},
    "household.item.done": {"item_id": 7},
    "household.item.remove": {"item_id": 7},
    "household.event.add": {"date": "2026-10-08", "time": "16:30", "title": "Dentist", "who": "self"},
    "household.event.remove": {"event_id": 3},
}


def test_wires_exactly_six_household_verbs(adapter):
    assert set(adapter.wire_handlers()) == set(VERBS)


# ---- authorization: only the server-injected operator flag ---------------------------------

NOT_OPERATOR = {
    "seat": {"token_verified": True, "stream_id": "amaterasu:v2-seat", "operator_authenticated": False},
    "nexus": {"token_verified": True, "stream_id": "amaterasu:v2-nexus", "role": "nexus",
              "operator_authenticated": False},
    "service": {"service_authenticated": True, "service_actor": "system:wmi", "operator_authenticated": False},
    "scoped": {"scoped_principal": True, "scope": {"stream": "partner:assistant"}, "operator_authenticated": False},
    "unauthenticated": {"operator_authenticated": False},
    "truthy-not-true": {"operator_authenticated": "yes", "operator_principal": "operator:cid"},
    # PENTACLE_SEAT_OPERATOR_AUTHORITY elevates a verified seat token exactly like this.
    "seat-operator-authority": {"token_verified": True, "stream_id": "amaterasu:v2-seat",
                                "operator_authenticated": True, "operator_principal": "agent:amaterasu:v2-seat",
                                "operator_authority_source": "stream_token"},
    "seat-authority-on-operator-principal": {"operator_authenticated": True, "operator_principal": "operator:cid",
                                             "operator_authority_source": "stream_token"},
    "operator-flag-without-principal": {"operator_authenticated": True},
    "missing": None,
}


@pytest.mark.parametrize("who", sorted(NOT_OPERATOR))
@pytest.mark.parametrize("verb", sorted(VERBS))
def test_non_operator_is_unauthorized_with_zero_cosmo_calls(adapter, cosmo, who, verb):
    from sessions import VerbError
    msg = {"type": verb, "request_id": "r1", **VERBS[verb]}
    if NOT_OPERATOR[who] is not None:
        msg["_auth_context"] = NOT_OPERATOR[who]
    with pytest.raises(VerbError) as exc:
        asyncio.run(adapter.wire_handlers()[verb](msg))
    assert code_of(exc) == "unauthorized"
    assert cosmo.calls == []


class Peer:
    def __init__(self, host):
        self.remote_address = (host, 40000)


def _server_with(adapter):
    from server import Server
    server = Server()
    server.handlers.update(adapter.wire_handlers())
    return server


def test_dispatch_strips_forged_auth_fields(adapter, cosmo):
    server = _server_with(adapter)
    forged = {"type": "household.snapshot", "request_id": "f1",
              "_auth_context": {"operator_authenticated": True}, "operator_authenticated": True}
    frames = asyncio.run(server._dispatch(json.dumps(forged), websocket=Peer("127.0.0.1")))
    assert frames[0]["type"] == "household.snapshot.error"
    # A top-level forged flag is an unknown field, the stripped _auth_context is ignored.
    assert frames[0]["error_code"] in {"unauthorized", "invalid_request"}
    forged.pop("operator_authenticated")
    frames = asyncio.run(server._dispatch(json.dumps(forged), websocket=Peer("127.0.0.1")))
    assert frames[0]["error_code"] == "unauthorized"
    assert cosmo.calls == []


def test_dispatch_operator_connection_reaches_cosmo(adapter, cosmo):
    server = _server_with(adapter)
    peer = Peer("192.0.2.9")
    server._connection_trust[peer] = operator_auth.ConnectionTrust("v2", "cid", "pentacle-mobile")
    frames = asyncio.run(server._dispatch(json.dumps({"type": "household.snapshot", "request_id": "o1"}),
                                          websocket=peer))
    assert frames[0]["type"] == "household.snapshot.ok", frames
    assert frames[0]["request_id"] == "o1"
    assert len(cosmo.calls) == 7


def test_dispatch_registered_on_real_server():
    from server import Server
    assert set(VERBS) <= set(Server().handlers)


# ---- request mapping -----------------------------------------------------------------------

def test_snapshot_defaults_to_chicago_month_and_next_week(adapter, cosmo):
    cosmo.routes[("GET", "/lists/tasks/items")] = (200, {"items": [item(1), item(2, done_at="2026-10-06T21:59:58Z")]})
    cosmo.routes[("GET", "/events")] = (200, {"items": [event(5, "2026-10-06"), event(4, "2026-10-01")],
                                              "server_now": "2026-10-07T03:00:00Z"})
    result = call(adapter, "household.snapshot")
    assert result["type"] == "household.snapshot.ok"
    assert result["today"] == "2026-10-06" and result["month"] == "2026-10"
    assert [i["id"] for i in result["lists"]["tasks"]] == [1]  # done (undo-window) rows dropped
    assert set(result["lists"]) == {"tasks", "grocery", "meals", "chores", "study"}
    # Merged across the two event ranges and de-duplicated by id, ordered by date.
    assert [e["id"] for e in result["events"]] == [4, 5]
    ranges = sorted((c["query"]["from"][0], c["query"]["to"][0]) for c in cosmo.calls if c["path"] == "/events")
    assert ranges == [("2026-10-01", "2026-10-31"), ("2026-10-06", "2026-10-13")]
    assert len(cosmo.calls) == 7
    assert all(c["method"] == "GET" for c in cosmo.calls)


def test_snapshot_other_month(adapter, cosmo):
    call(adapter, "household.snapshot", month="2027-02")
    ranges = sorted((c["query"]["from"][0], c["query"]["to"][0]) for c in cosmo.calls if c["path"] == "/events")
    assert ranges == [("2026-10-06", "2026-10-13"), ("2027-02-01", "2027-02-28")]


@pytest.mark.parametrize("month", ["1999-12", "2101-01", "2026-13", "2026-1", "2026-00", 202610, "", None])
def test_snapshot_invalid_month(adapter, cosmo, month):
    from sessions import VerbError
    with pytest.raises(VerbError) as exc:
        call(adapter, "household.snapshot", month=month)
    assert code_of(exc) == "invalid_range"
    assert cosmo.calls == []


def test_snapshot_month_bounds_inclusive(adapter, cosmo):
    assert call(adapter, "household.snapshot", month="2000-01")["month"] == "2000-01"
    assert call(adapter, "household.snapshot", month="2100-12")["month"] == "2100-12"


def test_item_add_maps_exactly_and_never_sends_scope(adapter, cosmo):
    cosmo.routes[("POST", "/lists/grocery/items")] = (201, {**item(9, "grocery"), "server_now": "n"})
    result = call(adapter, "household.item.add", list="grocery", label="  Limes ")
    assert result["type"] == "household.item.add.ok" and result["item"]["id"] == 9
    assert cosmo.calls == [{"method": "POST", "path": "/lists/grocery/items", "query": {},
                            "body": {"label": "Limes"}, "auth": f"Bearer {TOKEN}"}]


def test_item_done_and_remove(adapter, cosmo):
    cosmo.routes[("PATCH", "/lists/items/7/done")] = (200, item(7, done_at="2026-10-07T03:00:00Z"))
    cosmo.routes[("DELETE", "/lists/items/7")] = (204, b"")
    assert call(adapter, "household.item.done", item_id=7)["item"]["id"] == 7
    assert call(adapter, "household.item.remove", item_id=7) == {"type": "household.item.remove.ok", "item_id": 7}
    assert [(c["method"], c["path"], c["body"]) for c in cosmo.calls] == [
        ("PATCH", "/lists/items/7/done", None), ("DELETE", "/lists/items/7", None)]


def test_event_add_and_remove(adapter, cosmo):
    cosmo.routes[("POST", "/events")] = (201, event(3, "2026-10-08", time="16:30", who="both"))
    cosmo.routes[("DELETE", "/events/3")] = (204, b"")
    result = call(adapter, "household.event.add", date="2026-10-08", time="16:30", title=" Dentist ", who="both")
    assert result["type"] == "household.event.add.ok" and result["event"]["id"] == 3
    assert cosmo.calls[0]["body"] == {"date": "2026-10-08", "time": "16:30", "title": "Dentist", "who": "both"}
    assert result["event"]["who"] == "both" and result["event"]["scope"] == "private"
    call(adapter, "household.event.add", date="2026-10-08", time=None, title="All day", who="partner")
    assert cosmo.calls[1]["body"] == {"date": "2026-10-08", "time": None, "title": "All day", "who": "me"}
    call(adapter, "household.event.add", date="2026-10-08", time=None, title="Mine", who="self")
    assert cosmo.calls[2]["body"]["who"] == SELF
    assert call(adapter, "household.event.remove", event_id=3) == {"type": "household.event.remove.ok", "event_id": 3}


@pytest.mark.parametrize("verb,fields", [
    ("household.item.add", {"list": "grocery", "label": "x", "scope": "shared"}),
    ("household.item.add", {"list": "tasks", "label": "x", "priority": "hi"}),
    ("household.item.add", {"list": "tasks", "label": "x", "due_date": "2026-10-09"}),
    ("household.item.add", {"list": "tasks", "label": "x", "created_by": "bart"}),
    ("household.item.add", {"list": "wishlist", "label": "x"}),
    ("household.item.add", {"list": "tasks", "label": "   "}),
    ("household.item.add", {"list": "tasks", "label": "x" * 1001}),
    ("household.item.add", {"list": "tasks"}),
    ("household.item.done", {"item_id": "7"}),
    ("household.item.done", {"item_id": True}),
    ("household.item.done", {"item_id": 0}),
    ("household.item.remove", {"item_id": 7, "scope": "private"}),
    ("household.event.add", {"date": "2026-10-08", "time": None, "title": "x", "who": "both", "scope": "shared"}),
    ("household.event.add", {"date": "2026-10-08", "time": "4:30", "title": "x", "who": "self"}),
    ("household.event.add", {"date": "2026-02-30", "time": None, "title": "x", "who": "self"}),
    ("household.event.add", {"date": "2026-10-08", "time": None, "title": "x", "who": "owner"}),
    ("household.event.add", {"date": "2026-10-08", "title": "x", "who": "self"}),
    ("household.event.add", {"date": "2026-10-08", "time": None, "title": "", "who": "self"}),
    ("household.event.remove", {"event_id": -1}),
    ("household.snapshot", {"from": "2026-10-01"}),
])
def test_disallowed_or_invalid_fields_make_zero_cosmo_calls(adapter, cosmo, verb, fields):
    from sessions import VerbError
    with pytest.raises(VerbError) as exc:
        call(adapter, verb, **fields)
    assert code_of(exc) == "invalid_request"
    assert cosmo.calls == []


# ---- error matrix --------------------------------------------------------------------------

@pytest.mark.parametrize("status,code", [(404, "not_found"), (410, "not_found"), (422, "invalid_request"),
                                         (403, "forbidden"), (401, "unavailable")])
def test_mutation_status_mapping(adapter, cosmo, status, code):
    from sessions import VerbError
    cosmo.routes[("PATCH", "/lists/items/7/done")] = (status, {"detail": "echo of user text Limes"})
    with pytest.raises(VerbError) as exc:
        call(adapter, "household.item.done", item_id=7)
    assert code_of(exc) == code
    assert "Limes" not in str(exc.value) and TOKEN not in str(exc.value)
    assert len(cosmo.calls) == 1


def test_mutation_timeout_is_unknown_outcome_with_one_call(adapter, cosmo, monkeypatch):
    from sessions import VerbError
    import household
    monkeypatch.setattr(household, "CALL_TIMEOUT", 0.3)
    cosmo.routes[("POST", "/events")] = (201, event(3, "2026-10-08"))
    cosmo.delay[("POST", "/events")] = 1.0
    with pytest.raises(VerbError) as exc:
        call(adapter, "household.event.add", date="2026-10-08", time=None, title="x", who="self")
    assert code_of(exc) == "unknown_outcome"
    time.sleep(0.8)
    assert len(cosmo.calls) == 1  # never retried


def test_refused_connection_is_unavailable(token_file):
    from sessions import VerbError
    import socket
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    adapter = Household(url=f"http://127.0.0.1:{port}", token_file=str(token_file), clock=lambda: NOW, allow_insecure=True, self_person=SELF)
    for verb in ("household.snapshot", "household.item.remove"):
        with pytest.raises(VerbError) as exc:
            call(adapter, verb, **VERBS[verb])
        assert code_of(exc) == "unavailable"


def test_missing_token_file_is_unavailable_and_names_no_path(cosmo, tmp_path):
    from sessions import VerbError
    adapter = Household(url=cosmo.url, token_file=str(tmp_path / "absent.token"), clock=lambda: NOW, allow_insecure=True, self_person=SELF)
    with pytest.raises(VerbError) as exc:
        call(adapter, "household.snapshot")
    assert code_of(exc) == "unavailable"
    assert "absent.token" not in str(exc.value)
    assert cosmo.calls == []


def test_snapshot_slow_subcall_hits_deadline_all_or_nothing(adapter, cosmo, monkeypatch):
    from sessions import VerbError
    import household
    monkeypatch.setattr(household, "CALL_TIMEOUT", 5.0)
    monkeypatch.setattr(household, "SNAPSHOT_DEADLINE", 0.4)
    cosmo.delay[("GET", "/lists/study/items")] = 1.0
    async def run():
        started = time.monotonic()
        with pytest.raises(VerbError) as exc:
            await adapter.snapshot({"type": "household.snapshot", "_auth_context": OPERATOR})
        return code_of(exc), time.monotonic() - started
    code, elapsed = asyncio.run(run())
    assert code == "unavailable"
    assert elapsed < 0.95


def test_snapshot_oversized_body_is_unavailable(adapter, cosmo):
    from sessions import VerbError
    cosmo.routes[("GET", "/lists/meals/items")] = (200, b'{"items":[' + b'"x",' * 300000 + b'"x"]}')
    with pytest.raises(VerbError) as exc:
        call(adapter, "household.snapshot")
    assert code_of(exc) == "unavailable"


def test_snapshot_any_failed_subcall_is_unavailable(adapter, cosmo):
    from sessions import VerbError
    cosmo.routes[("GET", "/lists/chores/items")] = (500, {"detail": "boom"})
    with pytest.raises(VerbError) as exc:
        call(adapter, "household.snapshot")
    assert code_of(exc) == "unavailable"


# ---- token hygiene ---------------------------------------------------------------------------

def test_token_reaches_only_the_authorization_header(adapter, cosmo, token_file, caplog):
    caplog.set_level(logging.DEBUG)
    from sessions import VerbError
    frames = [call(adapter, "household.snapshot")]
    cosmo.routes[("DELETE", "/lists/items/7")] = (404, {"detail": "nope"})
    with pytest.raises(VerbError) as exc:
        call(adapter, "household.item.remove", item_id=7)
    frames.append(str(exc.value))
    assert all(c["auth"] == f"Bearer {TOKEN}" for c in cosmo.calls)
    assert TOKEN not in json.dumps(frames) and TOKEN not in caplog.text
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"


# ---- transport security and wire-level denials --------------------------------------------

@pytest.mark.parametrize("verb", sorted(VERBS))
def test_plain_http_url_is_refused_before_any_request(cosmo, token_file, verb, monkeypatch):
    from sessions import VerbError
    import household
    for adapter in (Household(url=cosmo.url, token_file=str(token_file), clock=lambda: NOW, self_person=SELF),):
        with pytest.raises(VerbError) as exc:
            call(adapter, verb, **VERBS[verb])
        assert code_of(exc) == "unavailable"
    monkeypatch.setenv(household.COSMO_URL_ENV, cosmo.url)
    with pytest.raises(VerbError) as exc:
        call(Household(token_file=str(token_file), clock=lambda: NOW, self_person=SELF), verb, **VERBS[verb])
    assert code_of(exc) == "unavailable"
    assert cosmo.calls == []


def test_unset_url_is_unavailable_with_zero_requests(cosmo, token_file, monkeypatch):
    from sessions import VerbError
    import household
    monkeypatch.delenv(household.COSMO_URL_ENV, raising=False)
    adapter = Household(token_file=str(token_file), clock=lambda: NOW, self_person=SELF)
    assert adapter.url == ""
    for verb in sorted(VERBS):
        with pytest.raises(VerbError) as exc:
            call(adapter, verb, **VERBS[verb])
        assert code_of(exc) == "unavailable"
    assert cosmo.calls == []


def test_env_https_url_is_used(token_file, monkeypatch):
    import household
    monkeypatch.setenv(household.COSMO_URL_ENV, "https://cosmo.example.test:8443/")
    assert Household(token_file=str(token_file)).url == "https://cosmo.example.test:8443"


def test_wire_remote_unauthenticated_is_denied_by_dispatcher(adapter, cosmo):
    server = _server_with(adapter)
    frames = asyncio.run(server._dispatch(json.dumps({"type": "household.item.remove", "request_id": "u1",
                                                      "item_id": 7}), websocket=Peer("192.0.2.10")))
    assert frames[0]["type"] == "household.item.remove.error"
    assert frames[0]["error_code"] == "authentication_required"
    assert cosmo.calls == []


def test_wire_seat_operator_authority_mode_still_denied(adapter, cosmo, monkeypatch):
    from server import Server
    server = Server(seat_operator_authority=True)
    server.handlers.update(adapter.wire_handlers())
    peer = Peer("192.0.2.11")

    async def seat_context(websocket, msg):
        # The exact shape `_auth_context` mints for a verified seat token in this mode.
        return {"stream_id": "amaterasu:v2-seat", "token_verified": True, "operator_authenticated": True,
                "operator_principal": "agent:amaterasu:v2-seat", "operator_authority_source": "stream_token",
                "service_authenticated": False, "service_attempted": False, "dot_principal": False,
                "transport_tls": False, "peer_loopback": False, "local_admin_verified": False}

    monkeypatch.setattr(server, "_auth_context", seat_context)
    frames = asyncio.run(server._dispatch(json.dumps({"type": "household.snapshot", "request_id": "s1"}),
                                          websocket=peer))
    assert frames[0]["error_code"] == "unauthorized"
    assert cosmo.calls == []


def test_seat_operator_authority_context_shape_matches_server():
    """Guard the predicate above against drift in server.py's elevation code."""
    import inspect
    from server import Server
    source = inspect.getsource(Server._auth_context)
    assert 'context["operator_authority_source"] = "stream_token"' in source
    assert 'f"agent:{owner}"' in source


# ---- neutral, viewer-relative vocabulary ---------------------------------------------------

def test_fake_cosmo_accepts_a_whole_snapshot_burst(cosmo):
    import household
    # One snapshot is len(LISTS) + 2 parallel calls; the fake must queue them all on every OS.
    assert cosmo.httpd.request_queue_size >= len(household.LISTS) + 2


def test_snapshot_translates_cosmo_vocabulary(adapter, cosmo):
    cosmo.routes[("GET", "/lists/tasks/items")] = (200, {"items": [
        item(1, created_by="bart"), item(2, scope="shared", created_by="helper"), item(3, created_by="app"),
        item(4, scope="someone-else"),  # never sent by Cosmo to this credential; dropped defensively
    ]})
    cosmo.routes[("GET", "/events")] = (200, {"items": [
        event(5, "2026-10-06", who="owner"), event(6, "2026-10-06", who="me", scope="shared"),
        event(7, "2026-10-06", who="both", created_by="bart"),
    ]})
    result = call(adapter, "household.snapshot")
    assert result["people"] == {"partner": "Sam"}
    assert [(i["id"], i["scope"], i["created_by"]) for i in result["lists"]["tasks"]] == [
        (1, "private", "assistant"), (2, "shared", "partner_assistant"), (3, "private", "app")]
    assert [(e["id"], e["who"], e["scope"], e["created_by"]) for e in result["events"]] == [
        (5, "self", "private", "app"), (6, "partner", "shared", "app"), (7, "both", "private", "assistant")]
    assert SELF not in json.dumps(result) and "bart" not in json.dumps(result) and "helper" not in json.dumps(result)


def test_partner_name_defaults_and_is_bounded(cosmo, token_file, monkeypatch):
    import household
    monkeypatch.delenv(household.PARTNER_NAME_ENV, raising=False)
    plain = Household(url=cosmo.url, token_file=str(token_file), clock=lambda: NOW, allow_insecure=True, self_person=SELF)
    assert call(plain, "household.snapshot")["people"] == {"partner": "Partner"}
    monkeypatch.setenv(household.PARTNER_NAME_ENV, "  " + "x" * 60 + " ")
    long = Household(url=cosmo.url, token_file=str(token_file), clock=lambda: NOW, allow_insecure=True, self_person=SELF)
    assert long.partner_name == "x" * 40


def test_unset_self_person_is_unavailable_with_zero_requests(cosmo, token_file, monkeypatch):
    from sessions import VerbError
    import household
    monkeypatch.delenv(household.COSMO_SELF_ENV, raising=False)
    adapter = Household(url=cosmo.url, token_file=str(token_file), clock=lambda: NOW, allow_insecure=True)
    for verb in sorted(VERBS):
        with pytest.raises(VerbError) as exc:
            call(adapter, verb, **VERBS[verb])
        assert code_of(exc) == "unavailable"
    assert cosmo.calls == []
