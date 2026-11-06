#!/usr/bin/env python3
"""Deterministic morning household digest; private runtime config, no model or writes to Cosmo.

Invoked by launchd every five minutes. See docs/household_due_reminders.md.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LISTS = ("tasks", "grocery", "meals", "chores", "study")
CT = ZoneInfo("America/Chicago")
MAX_BODY = 1 << 20
IDENTITY_ENV = ("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID", "AGENT_ORCH_STREAM_ID", "PENTACLE_STREAM_ID",
                "AGENT_ORCH_STREAM_TOKEN", "AGENT_ORCH_STREAM_TOKEN_FILE", "AGENT_ORCH_TOKEN")


class Failure(Exception):
    """Only fixed safe classes cross the diagnostic boundary."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def day_value(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise Failure("malformed_cosmo")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise Failure("malformed_cosmo") from None
    return value


def ident(value, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or value < 1:
        raise Failure("malformed_cosmo")
    return value


def private_file(path: Path) -> None:
    """Refuse symlinks and permissive credentials without reading their bytes."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise Failure("unsafe_private_file")
    except OSError:
        raise Failure("missing_private_file") from None


class Cosmo:
    def __init__(self, config):
        self.url = config["cosmo_url"].rstrip("/")
        self.token_file = Path(config["cosmo_token_file"]).expanduser()
        if not self.url.startswith("https://"):
            raise Failure("insecure_cosmo_url")

    def read(self, route):
        private_file(self.token_file)
        try:
            token = self.token_file.read_text().strip()
            if not token:
                raise Failure("missing_cosmo_token")
            request = urllib.request.Request(self.url + route, headers={"Authorization": "Bearer " + token})
            # Refuse redirects: a credential must never be forwarded to a different endpoint.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as reply:
                raw = reply.read(MAX_BODY + 1)
                if reply.status != 200 or len(raw) > MAX_BODY:
                    raise Failure("cosmo_read_failed")
            body = json.loads(raw)
        except (OSError, ValueError, urllib.error.URLError):
            raise Failure("cosmo_read_failed") from None
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise Failure("malformed_cosmo")
        return body["items"]


def collect(read, person: str, day: str):
    """Read the whole visible population; one retry for a cross-read identity race."""
    for attempt in range(2):
        lists = {name: read(f"/lists/{name}/items") for name in LISTS}
        routines = read("/routines")
        try:
            return select(lists, routines, person, day)
        except Failure as exc:
            if str(exc) != "inconsistent_cosmo" or attempt:
                raise


def select(lists, routines, person, day):
    items, routine_rows = {}, {}
    for name in LISTS:
        if not isinstance(lists.get(name), list):
            raise Failure("malformed_cosmo")
        for row in lists[name]:
            if not isinstance(row, dict) or not isinstance(row.get("scope"), str):
                raise Failure("malformed_cosmo")
            if row["scope"] not in (person, "shared"):
                continue
            key = ident(row.get("id"))
            if key in items or row.get("list") != name:
                raise Failure("inconsistent_cosmo")
            validate_label(row)
            if "done_at" not in row or (row["done_at"] is not None and not isinstance(row["done_at"], str)):
                raise Failure("malformed_cosmo")
            if row["done_at"] is not None:
                try:
                    if datetime.fromisoformat(row["done_at"]).tzinfo is None:
                        raise ValueError()
                except ValueError:
                    raise Failure("malformed_cosmo") from None
            if "due_date" not in row or "routine_id" not in row:
                raise Failure("malformed_cosmo")
            if row["due_date"] is not None:
                day_value(row["due_date"])
            ident(row["routine_id"], True)
            items[key] = row
    if not isinstance(routines, list):
        raise Failure("malformed_cosmo")
    for row in routines:
        if not isinstance(row, dict) or not isinstance(row.get("scope"), str):
            raise Failure("malformed_cosmo")
        if row["scope"] not in (person, "shared"):
            continue
        key = ident(row.get("id"))
        if key in routine_rows or row.get("list") not in LISTS:
            raise Failure("inconsistent_cosmo")
        validate_label(row)
        day_value(row.get("next_due"))
        if type(row.get("paused")) is not bool or "item_id" not in row:
            raise Failure("malformed_cosmo")
        ident(row["item_id"], True)
        routine_rows[key] = row
    references = {}
    for row in items.values():
        rid = row["routine_id"]
        if rid is not None:
            routine = routine_rows.get(rid)
            if (rid in references or not routine or routine["item_id"] != row["id"] or routine["list"] != row["list"]
                    or routine["scope"] != row["scope"] or routine["next_due"] != row["due_date"]):
                raise Failure("inconsistent_cosmo")
            references[rid] = row["id"]
    for rid, routine in routine_rows.items():
        if routine["item_id"] != references.get(rid):
            raise Failure("inconsistent_cosmo")
    rows = [{**row, "source": "item"} for row in items.values()
            if row["done_at"] is None and row["due_date"] is not None and row["due_date"] <= day]
    rows += [{**row, "source": "routine", "due_date": row["next_due"]}
             for row in routine_rows.values() if not row["paused"] and row["item_id"] is None and row["next_due"] <= day]
    return sorted(rows, key=lambda row: (row["due_date"], LISTS.index(row["list"]), row["id"], row["source"]))


def validate_label(row):
    if not isinstance(row.get("label"), str) or not row["label"].strip() or len(row["label"]) > 1000:
        raise Failure("malformed_cosmo")


def format_message(rows, day):
    current = date.fromisoformat(day)
    lines = [f"Due today · {current.strftime('%a, %b')} {current.day}"]
    for row in rows:
        # One source item always occupies one line, even with embedded line breaks.
        label = " ".join(row["label"].split())
        suffix = ""
        if row["due_date"] < day:
            due = date.fromisoformat(row["due_date"])
            suffix = f" (overdue since {due.strftime('%b')} {due.day})"
        lines.append(f"• {label}{suffix}")
    return "\n".join(lines)


class Publisher:
    """The installed CLI owns auth and RPC; this tool supplies only protected paths."""
    def __init__(self, config):
        self.config = config
        self.command = config["agent_orch"]
        endpoint = urlparse(config["ws_url"])
        if endpoint.scheme not in ("ws", "wss") or endpoint.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise Failure("rpc_not_local")
        self.base_env = {k: v for k, v in os.environ.items() if k not in IDENTITY_ENV}
        self.base_env.update(AGENT_ORCH_WS_URL=config["ws_url"], AGENT_ORCH_HOST_ID=config["host_id"])
        # This FD-installed job uses only the verified current FD seat credential.
        # Empty means the CLI must not load its default operator credential file.
        self.base_env["AGENT_ORCH_TOKEN"] = ""
        self.base_env.pop("TMUX", None)

    def rpc(self, args, env=None):
        try:
            child = subprocess.run([*self.command, *args, "--timeout", "12"], env=env or self.base_env,
                                   capture_output=True, text=True, timeout=18)
            body = json.loads(child.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise Failure("rpc_unconfirmed") from None
        if child.returncode != 0 or not isinstance(body, dict):
            raise Failure("rpc_refused")
        return body

    def _binding_hint(self):
        try:
            uri = Path(self.config["sessions_db"]).expanduser().resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as db:
                db.execute("PRAGMA query_only=ON")
                rows = db.execute("SELECT stream_id, generation, revision FROM v2_assistant_direct_binding WHERE name = ?",
                                  (self.config["binding_name"],)).fetchall()
            if len(rows) != 1:
                raise Failure("binding_hint_unavailable")
            stream, generation, revision = rows[0]
            if (not isinstance(stream, str) or not re.fullmatch(r"[^:\s]+:[^:\s]+", stream)
                    or not isinstance(generation, str) or not generation.strip()
                    or type(revision) is not int or revision < 0):
                raise Failure("binding_hint_unavailable")
            return {"stream_id": stream, "generation": generation, "revision": revision}
        except (OSError, sqlite3.Error, KeyError, ValueError):
            raise Failure("binding_hint_unavailable") from None

    def binding(self):
        # Local SQLite is only a discovery hint. No outbound action is authorized
        # until the same seat token authenticates a matching live binding read.
        hint = self._binding_hint()
        reply = self.rpc(["assistant", "binding"], self.seat_env(hint))
        if (reply.get("type") != "assistant.binding.ok"
                or any(reply.get(key) != hint[key] for key in ("stream_id", "generation", "revision"))
                or type(reply.get("revision")) is not int
                or reply.get("composite_stream_id") != self.config["composite_stream_id"]):
            raise Failure("binding_disagreement")
        return reply

    def seat_env(self, binding):
        stream = binding["stream_id"]
        if stream.count(":") != 1 or stream.split(":")[0] != self.config["host_id"]:
            raise Failure("binding_not_local")
        # launch.py::stream_token_file_for convention; no token copying or minting.
        key = digest(stream)[:24]
        path = Path(self.config["seat_cwd"]).expanduser() / ".pentacle-stream-tokens" / f"{key}.token"
        private_file(path)
        return {**self.base_env, "AGENT_ORCH_STREAM_ID": stream, "AGENT_ORCH_STREAM_TOKEN_FILE": str(path)}

    def publish(self, request_id, message):
        binding = self.binding()  # resolve again on every attempt, never cache a seat across handoffs
        reply = self.rpc(["assistant", "publish", "--proactive", "--composite-stream-id",
                          self.config["composite_stream_id"], "--publish-kind", "prose", "--response-state", "final",
                          "--request-id", request_id, "--message", message], self.seat_env(binding))
        if reply.get("type") != "assistant.publish.ok":
            raise Failure("publish_unconfirmed")
        return reply

    def tell(self, tell_id, message):
        binding = self.binding()
        reply = self.rpc(["tell", "assistant", message, "--tell-id", tell_id], self.seat_env(binding))
        if reply.get("type") != "tell.ok" or not (reply.get("action_committed") is True or reply.get("action_status") == "committed"
                                                  or reply.get("delivery_status") in ("persisted", "delivered", "landed")):
            raise Failure("tell_unconfirmed")
        return reply


def atomic_json(path: Path, value):
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def alert(state, day, error, evidence, namespace="household-due"):
    key = f"{namespace}:blocker:{day}:{error}"
    if key not in state["alerts"]:
        state["alerts"][key] = {"day": day, "error": error,
            "message": f"BLOCKER Household due reminders {day}: {error}; evidence {evidence}", "receipt": None}


def flush_alerts(state, save, publisher):
    for key, record in state["alerts"].items():
        if record["receipt"] is not None:
            continue
        try:
            record["receipt"] = publisher.tell(key, record["message"])
            save()
        except Failure:
            # Retain exactly the same tell ID/body until the timer gets a receipt.
            pass


def run(config, now, *, to_fd=False, state_root=None, reader=None, publisher=None):
    production = Path(config["state_root"]).expanduser().resolve()
    root = Path(state_root).expanduser().resolve() if state_root else production
    if to_fd and (state_root is None or root == production):
        raise Failure("test_state_required")
    if not to_fd and state_root is not None:
        raise Failure("production_state_override_refused")
    if not to_fd and not config.get("enabled", False):
        return {"status": "disabled"}
    alert_namespace = "household-due:fd-test:" + digest(str(root))[:24] if to_fd else "household-due"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Failure("unsafe_state_root")
    lock_path = root / "lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "locked"}
        path = root / "delivery.json"
        state = {"days": {}, "alerts": {}}
        if path.exists():
            private_file(path)
            try:
                state = json.loads(path.read_text())
                if not isinstance(state["days"], dict) or not isinstance(state["alerts"], dict):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise Failure("invalid_delivery_state") from None
        save = lambda: atomic_json(path, state)
        local = now.astimezone(CT)
        day = local.date().isoformat()
        first = date.fromisoformat(config["not_before"])
        publisher = publisher or Publisher(config)
        if not to_fd:
            missed = first
            while missed < local.date():
                prior_day = missed.isoformat()
                prior = state["days"].get(prior_day, {})
                if prior.get("status") not in ("delivered", "empty", "missed"):
                    state["days"][prior_day] = {**prior, "status": "missed"}
                    alert(state, prior_day, "missed", str(path), alert_namespace)
                missed += timedelta(days=1)
            save()
        flush_alerts(state, save, publisher)
        if local.date() < first or local.hour < 8:
            return {"status": "before_window"}
        record = state["days"].get(day, {})
        if record.get("status") in ("delivered", "empty", "missed"):
            return {"status": record["status"]}
        if local.hour >= 12:
            state["days"][day] = {**record, "status": "missed"}
            alert(state, day, "missed", str(path), alert_namespace)
            save()
            flush_alerts(state, save, publisher)
            return {"status": "missed"}
        try:
            if not record:
                reader = reader or Cosmo(config).read
                rows = collect(reader, config["self_person"], day)
                record = {"status": "pending" if rows else "empty", "day": day,
                          "request_id": f"household-due:{day}", "body": format_message(rows, day) if rows else "",
                          "sources": [{"source": r["source"], "id": r["id"]} for r in rows]}
                record["body_sha256"] = digest(record["body"])
                state["days"][day] = record
                save()  # freeze the body/key before the first outbound attempt
            if record["status"] == "empty":
                return {"status": "empty"}
            if record["body_sha256"] != digest(record["body"]) or record["request_id"] != f"household-due:{day}":
                raise Failure("invalid_delivery_state")
            receipt = (publisher.tell("household-due:rehearsal:" + day, "GATE Household reminder rehearsal\n" + record["body"])
                       if to_fd else publisher.publish(record["request_id"], record["body"]))
            record.update(status="delivered", receipt=receipt)
            save()
            return {"status": "delivered", "body_sha256": record["body_sha256"], "sources": record["sources"]}
        except Exception as exc:
            error = str(exc) if isinstance(exc, Failure) else "unexpected_failure"
            alert(state, day, error, str(path), alert_namespace)
            save()
            flush_alerts(state, save, publisher)
            return {"status": "failed", "error": error}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--as-of")
    parser.add_argument("--to-fd", action="store_true")
    parser.add_argument("--state-root")
    args = parser.parse_args(argv)
    try:
        if args.as_of and not args.to_fd:
            raise Failure("injected_time_requires_fd")
        private_file(args.config)
        config = json.loads(args.config.read_text())
        now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(CT)
        if now.tzinfo is None:
            raise Failure("timezone_required")
        result = run(config, now, to_fd=args.to_fd, state_root=args.state_root)
    except Exception as exc:
        result = {"status": "failed", "error": str(exc) if isinstance(exc, Failure) else "configuration_or_state_failure"}
    print(json.dumps({"subsystem": "household", "bug_ref": "household_due_reminders", **result}, sort_keys=True))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
