#!/usr/bin/env python3
"""Explicit, operator-commissioned recovery using existing daemon operations.

Private inputs and the private journal stay outside this repository. This tool
does not confer authority, manufacture consent, or change daemon policy.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid


class Refused(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def receipt_projection(reply):
    """Retain retry/readback identities without copying client/secret records."""
    result = {key:reply[key] for key in ("type","request_id","ok","duplicate","already_closed",
        "worker_stream_id","old_parent_stream_id","new_parent_stream_id","tell_id","delivery_status",
        "submission_confirmed","ledger_row_id","event_id","code") if key in reply}
    for key in ("intent","old_binding","new_binding","session","question","wake","watch"):
        record=reply.get(key)
        if isinstance(record,dict):
            result[key]={name:record[name] for name in ("request_id","question_id","id","state","status",
                "stream_id","generation","session_generation","target_stream_id","target_generation",
                "revision","expected_revision","owner_generation","child_generation","next_due_at") if name in record}
    return result


def private_read(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused("owner_only_regular_file_required")
    return json.loads(path.read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        private_read(path)
    fd, tmp = tempfile.mkstemp(prefix=".recovery-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
            raise Refused("unsafe_journal_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@contextmanager
def readonly_db(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.close()


def snapshot(inputs):
    """Select recovery facts only; never select tokens, consent keys or device ids."""
    old, generation = inputs["predecessor"], inputs["predecessor_generation"]
    with readonly_db(inputs["sessions_db"]) as conn:
        seats = [dict(row) for row in conn.execute("""SELECT s.host,s.session_name,s.status,
            s.parent_stream_id,s.role,s.visibility,s.pane_status,s.bootstrap_state,
            s.provider,s.effective_model,s.effective_effort,s.offline_since_ts,s.presumed_dead_at,
            s.spec_id,s.qualified_spec_ids,s.objective,
            g.generation AS session_generation FROM sessions s
            JOIN v2_session_generations g USING(host,session_name)""")]
        for seat in seats:
            seat["stream_id"] = seat["host"] + ":" + seat["session_name"]
        binding = dict(conn.execute("SELECT name,stream_id,generation,revision FROM "
            "v2_assistant_direct_binding WHERE name=?", (inputs["binding_name"],)).fetchone() or {})
        grant = dict(conn.execute("SELECT stream_id,session_generation,revision FROM "
            "v2_lifecycle_manager WHERE id=1").fetchone() or {})
        rows = [dict(row) for row in conn.execute("SELECT id,kind,child,child_generation,state,data "
            "FROM v2_watch_wake WHERE owner=? AND owner_generation=? ORDER BY id", (old, generation))]
        for row in rows:
            row["data"] = json.loads(row["data"])
        cancelled = [row[0] for row in conn.execute("SELECT target FROM v2_watch_cancellations "
            "WHERE owner=? AND owner_generation=?", (old, generation))]
        facts = [dict(row) for row in conn.execute("""SELECT f.notice_id,f.source,f.kind,
            n.delivered_at,n.terminal_at,n.terminal_reason FROM v2_watch_facts f
            LEFT JOIN v2_outbound_notices n USING(notice_id)
            WHERE f.owner=? AND f.owner_generation=?""", (old, generation))]
        ownership_table=inputs["authority_membership_table"]
        if not isinstance(ownership_table,str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",ownership_table):
            raise Refused("invalid_authority_membership_table")
        tracked = [dict(row) for row in conn.execute("SELECT target_stream_id,target_generation "
            f"FROM {ownership_table} WHERE target_stream_id=?", (old,))]
        pending_spawns = [dict(row) for row in conn.execute("SELECT host,session_name,request_id,expires_at "
            "FROM v2_stream_reservations WHERE json_extract(payload,'$.open_fields.parent_stream_id')=?", (old,))]
        lanes = [dict(row) for row in conn.execute("SELECT lane_id,phase,bound_stream_id,bound_generation,"
            "summary,pending_question_id,version,work_state,title,owner_kind FROM v2_assistant_composite_lanes "
            "WHERE stream_id=? AND (work_state IN ('active','paused','blocked') OR phase IN ('execution','waiting'))",
            (inputs.get("composite_stream", ""),))]
        routes = [dict(row) for row in conn.execute("SELECT input_identity,routing_state,delivery_state,dispatch_id,"
            "route_target,route_target_generation,error_code FROM v2_assistant_composite_routes WHERE stream_id=? "
            "ORDER BY updated_at DESC LIMIT 128", (inputs.get("composite_stream", ""),))]
        pending_routes = [dict(row) for row in conn.execute("""SELECT r.input_identity,r.routing_state,
            r.delivery_state,r.dispatch_id,r.route_target,r.route_target_generation,r.error_code
            FROM v2_assistant_composite_routes r WHERE r.stream_id=? AND NOT EXISTS
            (SELECT 1 FROM v2_assistant_composite_publications p WHERE p.dispatch_id=r.dispatch_id
             AND json_extract(p.canonical_payload_json,'$.response_state')='final')""",
            (inputs.get("composite_stream", ""),))]
    questions = []
    if inputs.get("notifications_db"):
        with readonly_db(inputs["notifications_db"]) as conn:
            for row in conn.execute("SELECT q.question_id,q.state,q.envelope,q.answer,n.resolution,q.producer_session_generation "
                    "FROM agent_questions q LEFT JOIN notifications n ON n.notification_id=q.notification_id "
                    "WHERE q.producer_stream_id=? AND (q.producer_session_generation=? OR q.producer_session_generation IS NULL)", (old, generation)):
                question = dict(row)
                for key in ("envelope", "answer", "resolution"):
                    if isinstance(question[key], str):
                        question[key] = json.loads(question[key])
                # Preserve the business answer and its daemon pointer, not the
                # paired client's credential identity in the private journal.
                if isinstance(question.get("answer"),dict):
                    question["answer"] = {k:v for k,v in question["answer"].items() if k != "operator_auth"}
                    question["envelope"]["answer"] = question["answer"]
                if isinstance(question.get("resolution"),dict):
                    question["resolution"] = {k:v for k,v in question["resolution"].items()
                        if k not in {"actor_stream_id","operator_auth","credential_id","device_id"}}
                questions.append(question)
    lookup = {seat["stream_id"]: seat for seat in seats}
    predecessor = lookup.get(old)
    parent = lookup.get((predecessor or {}).get("parent_stream_id"))
    return {"binding": binding, "grant": grant, "predecessor": predecessor,
            "parent": parent, "seats": seats, "subscriptions": rows,
            "explicit_cancellations": cancelled, "facts": facts, "questions": questions,
            "lanes":lanes,"pending_route_references":pending_routes,
            "pending_spawns":pending_spawns,
            "recent_route_references":routes,"history_stream":inputs.get("composite_stream"),
            "tracked_target": any(r["target_generation"] == generation for r in tracked)}


def retained_plan(state):
    cancelled = set(state["explicit_cancellations"])
    retained = []
    for row in state["subscriptions"]:
        data = row["data"]
        if row["id"] in cancelled or data.get("default"):
            continue
        consumed = data.get("consumed", {})
        remaining = [t for t in data.get("triggers", []) if t not in consumed]
        notices = {v.get("notice_id") for v in consumed.values() if isinstance(v, dict)}
        facts = [f for f in state["facts"] if f["notice_id"] in notices]
        if row["kind"] == "wake":
            if consumed:
                if facts and all(f["delivered_at"] for f in facts):
                    # Submission is known; business execution is not inferred.
                    retained.append({**row, "disposition": "submitted_execution_uncertain"})
                else:
                    retained.append({**row, "disposition": "enqueued_delivery_uncertain"})
            else:
                retained.append({**row, "disposition": "restore"})
        elif remaining:
            retained.append({**row, "remaining_triggers": remaining, "disposition": "restore"})
    return retained


def selected(root, keys):
    for key in keys:
        root = root[key]
    return root


def replace_selected(root, keys, value):
    parent = root
    for key in keys[:-1]:
        parent = parent[key]
    parent[keys[-1]] = value


def command(argv, inputs=None):
    """Private outputs are captured, never forwarded to logs or exceptions."""
    result = subprocess.run(argv, input=None if inputs is None else json.dumps(inputs),
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise Refused("private_adapter_failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def route_files(inputs):
    """Daemon-host adapter. Restore selected fields, never a whole secret config."""
    receipts = []
    for route in inputs["routes"]:
        path = Path(route["path"])
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Refused("unsafe_route_file")
        raw = path.read_bytes()
        loader = plistlib.loads if route["format"] == "plist" else json.loads
        root = loader(raw)
        current = [selected(root, field["keys"]) for field in route["fields"]]
        expected = [field["before"] for field in route["fields"]]
        target = [field["after"] for field in route["fields"]]
        if current not in (expected, target):
            raise Refused("route_field_conflict")
        if current != target:
            # If a write races another owner, abort instead of overwriting it.
            for field in route["fields"]:
                replace_selected(root, field["keys"], field["after"])
            encoded = plistlib.dumps(root) if route["format"] == "plist" else (json.dumps(root, indent=2)+"\n").encode()
            fd, tmp = tempfile.mkstemp(prefix=".route-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(encoded); out.flush(); os.fsync(out.fileno())
                os.chmod(tmp, stat.S_IMODE(info.st_mode))
                if path.read_bytes() != raw:
                    raise Refused("route_file_changed")
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        if not route.get("verify_command"):
            raise Refused("effective_route_readback_required")
        check = command(route["verify_command"])
        if check != route["expected_readback"] and route.get("activate_command"):
            command(route["activate_command"])
            check = command(route["verify_command"])
        if check != route["expected_readback"]:
            raise Refused("effective_route_mismatch")
        receipts.append({"id": route["id"], "passed": True,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"passed": True, "routes": receipts}


class Gateway:
    def __init__(self, manifest):
        from agent_orch.config import load_config
        self.manifest, self.config = manifest, load_config()
        if self.config.ws_url != manifest["endpoint"]:
            raise Refused("endpoint_mismatch")
        self.actor = os.environ.get("AGENT_ORCH_STREAM_ID") or os.environ.get("PENTACLE_STREAM_ID")
        if not self.actor:
            raise Refused("authenticated_actor_required")

    async def rpc(self, payload):
        from agent_orch.wsclient import _one_shot_rpc, _stream_token_from_env
        payload = dict(payload)
        if not payload["type"].startswith("assistant."):
            payload["from_stream_id"] = self.actor
            payload["stream_token"] = _stream_token_from_env()
        return await _one_shot_rpc(self.config, payload, prefix=payload["type"],
                                  from_stream_id=self.actor, timeout=30)

    async def snapshot(self):
        inputs = {**self.manifest["snapshot_inputs"], "predecessor":self.manifest["predecessor"],
                  "predecessor_generation":self.manifest["predecessor_generation"],
                  "binding_name":self.manifest["binding_name"],"composite_stream":self.manifest["composite_stream"]}
        state = await asyncio.to_thread(command, self.manifest["snapshot_command"], inputs)
        binding = await self.rpc({"type":"assistant.binding", "request_id":uuid.uuid4().hex,
                                  "composite_stream_id":self.manifest["composite_stream"]})
        if not binding.get("type", "").endswith(".ok"):
            raise Refused("binding_readback_failed")
        state["binding"] = {key:binding[key] for key in ("stream_id","generation","revision")}
        authority = await self.rpc({"type":"assistant.authority","action":"read","request_id":uuid.uuid4().hex})
        if not authority.get("type", "").endswith(".ok"):
            raise Refused("fixed_authority_readback_failed")
        state["fixed_authority"] = {key:authority.get(key) for key in ("state","stream_id","generation","source")}
        return state

    async def routes(self, inputs):
        return await asyncio.to_thread(command, self.manifest["route_command"], inputs)

    async def spawn(self, payload):
        from agent_orch.wsclient import _one_shot_rpc, _stream_token_from_env, _await_starting_spawn
        # Authenticate the independent executor without parenting the target.
        # The existing CLI's --top-level path omits inferred seat identity.
        payload={**payload,"from_stream_id":self.actor,"stream_token":_stream_token_from_env()}
        response=await _one_shot_rpc(self.config,payload,prefix="spawn",from_stream_id=self.actor,timeout=180)
        if response.get("type")=="spawn.ok" and response.get("state")=="starting":
            response=await _await_starting_spawn(self.config,response,deadline=time.monotonic()+180)
        return response



class Recovery:
    def __init__(self, manifest, gateway, journal_path):
        self.m, self.g, self.path = manifest, gateway, Path(journal_path)
        self.j = private_read(self.path) if self.path.exists() else {"manifest_digest":digest(manifest), "receipts":{}}
        if self.j["manifest_digest"] != digest(manifest):
            raise Refused("manifest_changed")

    def commit(self):
        save(self.path, self.j)

    def identity(self, key):
        return "recovery-" + digest([self.m["operation_id"], key])[:32]

    def require(self, action):
        if not self.m.get("authorization", {}).get(action):
            raise Refused("missing_"+action+"_authorization")

    async def effect(self, key, payload):
        if key in self.j["receipts"]:
            return self.j["receipts"][key]
        payload = {**payload, "request_id":self.identity(key)}
        previous = self.j.setdefault("intents", {}).get(key)
        if previous and previous != digest(payload):
            raise Refused("operation_intent_changed")
        self.j["intents"][key] = digest(payload)
        self.commit()  # Commit intent before submitting an effect.
        reply = await self.g.rpc(payload)
        if not reply.get("type", "").endswith(".ok") or reply.get("ok") is False:
            self.j["pending"] = {"stage":key, "code":reply.get("error_code",reply.get("type"))}
            self.commit()
            raise Refused("operation_deferred:"+key)
        # Store only operation receipts, never the payload's private prose/token.
        self.j["receipts"][key] = receipt_projection(reply)
        self.j.pop("pending", None)
        self.commit()
        return self.j["receipts"][key]

    async def plan(self):
        state = await self.g.snapshot()
        old = state["predecessor"]
        if not old or old["session_generation"] != self.m["predecessor_generation"]:
            raise Refused("predecessor_generation_changed")
        return {"dry_run":True, "binding":state["binding"], "grant":state["grant"],
                "children":sum(s["status"] == "open" and s["parent_stream_id"] == self.m["predecessor"] for s in state["seats"]),
                "retained_subscriptions":len(retained_plan(state)), "question_obligations":len(state["questions"]),
                "tracked_target":state["tracked_target"], "pending_spawns":state["pending_spawns"], "mutations":0}

    async def advance(self):
        self.require("rebind")
        state = await self.g.snapshot()
        actor = next((s for s in state["seats"] if s["stream_id"] == self.g.actor), None)
        if not actor or actor["status"] != "open" or actor["pane_status"] != "pane_alive":
            raise Refused("successor_not_ready")
        target = self.m["target"]
        if any(actor[field] != target[key] for field,key in
               (("host","host"),("provider","provider"),("effective_model","model"),("effective_effort","effort"))):
            raise Refused("successor_target_tuple_mismatch")
        old = state["predecessor"]
        if not old or old["session_generation"] != self.m["predecessor_generation"]:
            raise Refused("predecessor_generation_changed")
        if "successor" not in self.j:
            if state["binding"]["stream_id"] != self.m["predecessor"] or state["binding"]["generation"] != self.m["predecessor_generation"]:
                raise Refused("foreign_binding")
            self.j.update(successor=actor["stream_id"], generation=actor["session_generation"],
                          original_binding=state["binding"], original_grant=state["grant"],bootstrap=state)
            self.commit()
        if (self.j["successor"],self.j["generation"]) != (actor["stream_id"],actor["session_generation"]):
            raise Refused("successor_generation_changed")
        new = (actor["stream_id"], actor["session_generation"])
        binding = state["binding"]
        if (binding["stream_id"],binding["generation"]) not in (new,(self.m["predecessor"],self.m["predecessor_generation"])):
            raise Refused("foreign_binding")
        # Request the exact consent early. A daemon rejection is a real blocker.
        if "consent" not in self.j["receipts"]:
            await self.effect("consent", {"type":"consent.request", "action":"lifecycle.designate",
                "target_stream_id":new[0],"target_generation":new[1],
                "expected_revision":self.j["original_grant"].get("revision",0),
                "reason":"Designate the explicitly commissioned recovery successor"})
        intent = self.j["receipts"]["consent"].get("intent",{})
        if (intent.get("target_stream_id"),intent.get("target_generation")) != new:
            raise Refused("consent_target_mismatch")
        # One reconciliation at an ordinary turn boundary; no timer/poll loop.
        status = await self.g.rpc({"type":"consent.status","request_id":uuid.uuid4().hex,
                                   "intent_id":intent["request_id"]})
        if not status.get("type", "").endswith(".ok"):
            raise Refused("consent_status_unavailable")
        current_intent = status.get("intent",{})
        if (current_intent.get("target_stream_id"),current_intent.get("target_generation")) != new:
            raise Refused("consent_status_target_changed")
        if current_intent.get("state") not in ("pending","approved"):
            raise Refused("consent_"+str(current_intent.get("state")))
        if (binding["stream_id"],binding["generation"]) != new:
            await self.effect("rebind", {"type":"assistant.rebind", "composite_stream_id":self.m["composite_stream"],
                "target_stream_id":new[0],"target_generation":new[1],"expected_revision":binding["revision"]})
        state = await self.g.snapshot()
        if (state["binding"]["stream_id"],state["binding"]["generation"]) != new:
            raise Refused("rebind_readback_failed")
        grant = state["grant"]
        if (grant.get("stream_id"),grant.get("session_generation")) != new:
            self.j["state"] = "answering_pending_consent"
            self.commit()
            return {"state":self.j["state"],"binding":state["binding"],"lifecycle_ready":False}
        if grant["revision"] != self.j["original_grant"].get("revision",0)+1:
            raise Refused("grant_revision_changed")
        if self.j.get("state") in ("recovered_pending_mirror_verification","verified"):
            return {"state":self.j["state"],"binding":state["binding"],"lifecycle_ready":True,
                    "duplicate":True,"routes":self.j["routes"]}
        self.require("retire")
        if "sealed" not in self.j:
            self.j["sealed"] = state
            self.j["transfer"] = retained_plan(state)
            self.commit()
        sealed = self.j["sealed"]
        self.fence_watches(state)
        children = [s for s in sealed["seats"] if s["parent_stream_id"] == self.m["predecessor"] and s["status"] == "open"]
        for child in children:
            current = next((s for s in state["seats"] if s["stream_id"] == child["stream_id"]), None)
            if not current or current["session_generation"] != child["session_generation"]:
                raise Refused("child_generation_changed")
            if current["status"] != "open":
                continue  # Completed child is retained in the sealed receipt.
            if current["parent_stream_id"] not in (self.m["predecessor"],new[0]):
                raise Refused("child_parent_changed")
            if current["parent_stream_id"] != new[0]:
                await self.effect("adopt:"+child["stream_id"], {"type":"reparent", "stream_id":child["stream_id"],
                    "new_parent_stream_id":new[0],"expected_generation":child["session_generation"],
                    "reason":"Adopt the sealed recovery child inventory"})
        parent = sealed["parent"]
        if parent and parent["status"] == "open":
            await self.effect("successor-parent", {"type":"reparent","stream_id":new[0],
                "new_parent_stream_id":parent["stream_id"],"expected_generation":new[1],
                "reason":"Restore the open predecessor ancestry"})
        state = await self.g.snapshot()
        if state.get("fixed_authority") != sealed.get("fixed_authority"):
            raise Refused("fixed_authority_changed")
        if any(s["parent_stream_id"] == self.m["predecessor"] and s["status"] == "open" for s in state["seats"]):
            raise Refused("new_child_or_pending_adoption")
        expected = {r["id"]:r for r in sealed["subscriptions"]}
        for row in state["subscriptions"]:
            if row["id"] not in expected:
                raise Refused("subscription_inventory_changed")
        for row in self.j["transfer"]:
            if any(key+row["id"] in self.j["receipts"] for key in ("wake:","watch:","obligation:")):
                continue
            # Explicit late cancellation must not be resurrected.
            if row["id"] in state["explicit_cancellations"]:
                self.j.setdefault("cancelled_after_seal",[]).append(row["id"])
        self.j["before_close"] = state
        self.retain_questions(state)
        self.commit()
        old = state["predecessor"]
        if old["status"] != "closed":
            await self.effect("retire", {"type":"close","stream_id":self.m["predecessor"],
                "expected_generation":self.m["predecessor_generation"],
                "reason":"Retire the explicitly gated recovery predecessor",
                "disposition_waived_reason":"Recovery replaces coordination; sealed task obligations retained"})
        state = await self.g.snapshot()
        if state["predecessor"]["status"] != "closed" or any(r["state"] == "active" for r in state["subscriptions"]):
            raise Refused("retirement_readback_pending")
        self.retain_questions(state)
        self.commit()
        # Final old-owner consumption/answer receipts win over sealed copies.
        latest = {r["id"]:r for r in retained_plan(state)}
        for row in self.j["transfer"]:
            if row["kind"] == "watch" and "watch:"+row["id"] in self.j["receipts"]:
                await self.check_watch_receipt(row)
            if any(key+row["id"] in self.j["receipts"] for key in ("wake:","watch:","obligation:")):
                continue
            if row["id"] in self.j.get("cancelled_after_seal",[]) or row["id"] in state["explicit_cancellations"]:
                continue
            if row["id"] not in latest:
                continue  # Consumed custom triggers must not be resurrected.
            row = latest[row["id"]]
            data = row["data"]
            if row["disposition"] != "restore" or (row["kind"] == "wake" and data["due_at"] <= time.time()):
                await self.effect("obligation:"+row["id"], {"type":"tell","to_stream_id":new[0],
                    "tell_id":str(uuid.uuid5(uuid.NAMESPACE_URL,self.identity(row["id"]))),
                    "text":"Retained recovery obligation: reconcile prior work before repeating it. " + data.get("note", "")})
            elif row["kind"] == "wake":
                await self.effect("wake:"+row["id"], {"type":"wake.register",
                    "at":datetime.fromtimestamp(data["due_at"],timezone.utc).isoformat(),
                    "note":data.get("note", ""),"urgent":data.get("urgent",False)})
            else:
                self.fence_watches(await self.g.snapshot(), [row])
                await self.effect("watch:"+row["id"], {"type":"watch.register", "child_stream_id":row["child"],
                    "on":row.get("remaining_triggers",data["triggers"]),"repeat":data.get("repeat",False)})
                await self.check_watch_receipt(row)
        await self.questions(state)
        self.require("routes")
        routes = self.m["routes"]
        substitutions = {"$successor_stream":new[0],"$successor_generation":new[1]}
        route_inputs = json.loads(json.dumps({"routes":routes}))
        for route in route_inputs["routes"]:
            for field in route["fields"]:
                field["after"] = substitutions.get(field["after"],field["after"])
            route["expected_readback"] = {k:substitutions.get(v,v) for k,v in route["expected_readback"].items()}
        self.j["route_plan"] = route_inputs
        self.commit()
        receipt = await self.g.routes(route_inputs)
        if not receipt.get("passed"):
            raise Refused("routes_incomplete")
        self.j["routes"] = receipt
        self.j["state"] = "recovered_pending_mirror_verification"
        self.commit()
        return {"state":self.j["state"],"binding":state["binding"],"lifecycle_ready":True,
                "restored_subscriptions":len(self.j["transfer"]),"routes":receipt}

    async def verify(self):
        if self.j.get("state") not in ("recovered_pending_mirror_verification","verified"):
            raise Refused("recovery_stages_incomplete")
        state = await self.g.snapshot()
        new = (self.j["successor"],self.j["generation"])
        if (state["binding"]["stream_id"],state["binding"]["generation"]) != new:
            raise Refused("verification_binding_changed")
        check = await asyncio.to_thread(command, self.m["mirror_command"],
            {"operation_id":self.m["operation_id"],"stream_id":new[0],"generation":new[1],
             "composite_stream":self.m["composite_stream"]})
        if not check.get("passed") or check.get("final_count") != 1 or check.get("stream_id") != new[0] or check.get("generation") != new[1]:
            raise Refused("canonical_mirror_verification_failed")
        self.j["mirror"] = check
        self.j["state"] = "verified"
        self.commit()
        return {"state":"verified","binding":state["binding"],"mirror":check}

    async def start(self):
        """One fenced existing spawn; files are pre-staged, not copied secretly."""
        self.require("spawn")
        if "spawn" in self.j:
            return self.j["spawn"]
        target = self.m["target"]
        script, manifest, journal = (target[k] for k in ("script","manifest","journal"))
        invocation=shlex.join([target["python"],script,"advance","--manifest",manifest,"--journal",journal])
        brief = ("Explicitly commissioned recovery front desk. Use your own credential, never the predecessor's. "
                 "Read the private recovery manifest and checkpoint pointer; acknowledge READY by running " +
                 invocation + ". Read bootstrap state from the private journal while consent is pending; "
                 "At the start of each ordinary dispatch or tell turn run advance once until recovery completes. "
                 "It checks consent.status once; never poll or create a timer/manual wake. "
                 "After it returns with recovered_pending_mirror_verification run the same command with verify. "
                 "Answer composite dispatches after rebind even while lifecycle designation is pending. "
                 "If it refuses, report the stage and preserve every fence; no new consent card or force-close. "
                 "Never restart or move the daemon. Your commission is limited by the manifest's explicit grants.")
        if target.get("bootstrap_brief"):
            brief = "First read the private bootstrap brief at " + shlex.quote(target["bootstrap_brief"]) + ". " + brief
        from _shared.spawn_profiles import resolve_spawn
        payload={"type":"spawn",**resolve_spawn(provider=target["provider"],model=target["model"],
            effort=target["effort"],host=target["host"]),"host":target["host"],"role":"lead","visibility":"default",
            "objective":"Recover the explicitly commissioned assistant front desk","objective_supported":True,
            "initial_prompt":brief,"spec_id":self.m["spawn_spec_ids"][0],"spec_ids":self.m["spawn_spec_ids"],
            "request_id":self.identity("spawn"),"idempotency_key":self.identity("spawn")}
        prior=self.j.get("spawn_intent_digest")
        if prior and prior != digest(payload):
            raise Refused("spawn_intent_changed")
        self.j["spawn_intent_digest"]=digest(payload)
        self.commit()
        response=await self.g.spawn(payload)
        if response.get("state") != "ready" or response.get("admitted_count") != 1:
            self.j["pending"]={"stage":"spawn","code":response.get("type"),
                                "ruling_request_id":response.get("ruling_request_id")}
            self.commit()
            raise Refused("spawn_not_ready_reconcile_same_intent")
        seat=response["session"]
        if seat.get("parent_stream_id") or seat["visibility"] != "default" or (seat["effective_model"],seat["effective_effort"]) != (target["model"],target["effort"]):
            raise Refused("spawn_topology_or_tuple_mismatch")
        self.j["spawn"]={"stream_id":response["stream_id"],"generation":seat["session_generation"],
                         "request_id":self.identity("spawn"),"state":"ready"}
        self.commit()
        return self.j["spawn"]

    def fence_watches(self, state, rows=None):
        seats = {s["stream_id"]:s for s in state["seats"]}
        for row in self.j["transfer"] if rows is None else rows:
            if row["kind"] != "watch" or row["id"] in state["explicit_cancellations"]:
                continue
            child = seats.get(row["child"])
            if not child or child["session_generation"] != row["child_generation"]:
                raise Refused("watched_child_generation_changed")

    async def check_watch_receipt(self, row):
        receipt = self.j["receipts"]["watch:"+row["id"]]["watch"]
        if receipt.get("child_generation") != row["child_generation"]:
            # Existing registration has no expected-child-generation argument.
            # Cancel a raced registration, retain the mismatch, and stop.
            await self.effect("cancel-raced-watch:"+row["id"],
                {"type":"watch.cancel","id":receipt["id"]})
            raise Refused("restored_watch_generation_changed")

    def retain_questions(self, state):
        obligations = self.j.setdefault("question_obligations", {})
        for source in (self.j["sealed"], state):
            for question in source["questions"]:
                obligations.setdefault(question["question_id"], question)

    async def questions(self, state):
        latest = {q["question_id"]:q for q in state["questions"]}
        for prior in self.j["question_obligations"].values():
            q = latest.get(prior["question_id"],prior)
            key = "question:"+q["question_id"]
            context = prior["envelope"].get("context")
            if isinstance(context,str):
                try:
                    context=json.loads(context)
                except ValueError:
                    context=None
            if prior["envelope"].get("_assistant_composite_question_proxy") is True:
                self.j.setdefault("question_dispositions",{})[key] = "durable_composite_question_retained"
            elif q.get("answer"):
                self.j.setdefault("question_dispositions",{})[key] = "accepted_answer_retained"
            elif not prior.get("producer_session_generation"):
                self.j.setdefault("question_dispositions",{})[key] = "legacy_generation_obligation_review_required"
            elif isinstance(context,dict) and context.get("schema") == "HandoffModelChangeApprovalV1":
                self.j.setdefault("question_dispositions",{})[key] = "generation_bound_approval_requires_fresh_request"
            elif q["question_id"] in self.m.get("obsolete_questions",[]):
                self.j.setdefault("question_dispositions",{})[key] = "obsolete"
            elif (prior["state"] in ("open","pending")
                    or (prior.get("resolution") or {}).get("reason") == "asker_closed"
                    or self.m.get("recover_expired_questions",False)):
                requests = self.j.setdefault("question_requests", {})
                if key not in requests:
                    envelope = dict(prior["envelope"])
                    envelope.pop("producer_session_generation",None)
                    envelope.update(question_id="q-"+digest([self.m["operation_id"],q["question_id"]])[:16],
                        producer_stream_id=self.j["successor"],producer_provider=self.m["target"]["provider"],
                        dedup_key=self.identity(key),answer=None,answered_at=None,
                        created_at=datetime.now(timezone.utc).isoformat(),updated_at=datetime.now(timezone.utc).isoformat())
                    requests[key] = envelope
                    self.commit()
                envelope = requests[key]
                from agent_orch.prompt_protocol import notification_actions
                await self.effect(key,{"type":"prompt.ask","envelope":envelope,"actions":notification_actions(envelope)})
                self.j.setdefault("question_dispositions",{})[key] = "recreated"
            else:
                self.j.setdefault("question_dispositions",{})[key] = "closed_obligation_review_required"
            self.commit()

    async def rollback(self):
        self.require("rollback")
        state = await self.g.snapshot()
        if not state["predecessor"] or state["predecessor"]["status"] != "open" or state["predecessor"]["pane_status"] != "pane_alive":
            raise Refused("dead_predecessor_requires_roll_forward")
        grant = state["grant"]
        if grant != self.j["original_grant"]:
            raise Refused("authority_rollback_requires_new_phone_consent")
        consent = self.j.get("receipts",{}).get("consent",{}).get("intent",{})
        if consent:
            await self.effect("rollback-consent", {"type":"consent.cancel","intent_id":consent["request_id"]})
        if self.j.get("route_plan"):
            reverse = json.loads(json.dumps(self.j["route_plan"]))
            for route in reverse["routes"]:
                for field in route["fields"]:
                    field["before"],field["after"] = field["after"],field["before"]
                route["expected_readback"] = route["rollback_readback"]
            await self.g.routes(reverse)
        await self.effect("rollback-binding", {"type":"assistant.rebind","composite_stream_id":self.m["composite_stream"],
            "target_stream_id":self.m["predecessor"],"target_generation":self.m["predecessor_generation"],
            "expected_revision":state["binding"]["revision"]})
        final = await self.g.snapshot()
        if final["binding"]["stream_id"] != self.m["predecessor"] or final["binding"]["generation"] != self.m["predecessor_generation"]:
            raise Refused("rollback_readback_failed")
        self.j["state"] = "rolled_back_before_designation"
        self.commit()
        return {"state":self.j["state"],"binding":final["binding"]}


async def main(args):
    if args.action in ("snapshot","route-files"):
        inputs = json.load(sys.stdin)
        return snapshot(inputs) if args.action == "snapshot" else route_files(inputs)
    manifest = private_read(args.manifest)
    gateway = Gateway(manifest)
    recovery = Recovery(manifest,gateway,args.journal)
    if args.action == "plan":
        return await recovery.plan()
    with locked(args.journal):
        if args.action == "start":
            return await recovery.start()
        if args.action == "verify":
            return await recovery.verify()
        if args.action == "rollback":
            return await recovery.rollback()
        result = await recovery.advance()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("plan","start","advance","verify","rollback","snapshot","route-files"))
    parser.add_argument("--manifest",type=Path)
    parser.add_argument("--journal",type=Path)
    args = parser.parse_args()
    if args.action not in ("snapshot","route-files") and (not args.manifest or not args.journal):
        parser.error("--manifest and --journal are required")
    try:
        print(json.dumps(asyncio.run(main(args)),sort_keys=True))
    except (Refused,TimeoutError,OSError,ValueError,KeyError,sqlite3.Error) as exc:
        # Do not emit exception strings from transports/adapters/private files.
        print(json.dumps({"state":"incomplete","code":str(exc) if isinstance(exc,Refused) else type(exc).__name__}))
        raise SystemExit(2)
