#!/usr/bin/env python3
"""A bounded daily retro producer using the existing worker/report transport."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from time import monotonic
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SERVICES = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICES / "agent-orch"))
sys.path.insert(0, str(SERVICES))
sys.path.insert(0, str(SERVICES / "chat-stream-v2"))
from agent_orch.config import Config  # noqa: E402
from agent_orch.triage import parse_frontmatter  # noqa: E402
from agent_orch import prompt_protocol, wsclient  # noqa: E402
from tools.live_window import authenticated_operator_connection  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
DISPOSITIONS = {"resolved", "duplicate", "no_change", "investigate", "authorized", "propose", "defer"}
STAGES = ("sol", "astra", "final-astra")
ATTENTION = {"no_owning_work", "new_grant", "recurrence", "changed_evidence", "ownership_gap", "revised_action", "uncertain"}
CHANGED = {"recurrence", "changed_evidence", "ownership_gap", "revised_action"}
PROPOSAL_START = "<!-- daily-retro-proposals -->"
PROPOSAL_END = "<!-- /daily-retro-proposals -->"


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".retro-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value if isinstance(value, bytes) else encoded(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@dataclass(frozen=True)
class Settings:
    memory_root: Path
    state_root: Path
    ws_url: str
    token_path: Path
    host: str
    isolated: bool = False
    config_path: Path | None = None

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = read(path)
        if data.get("timezone") != ZONE.key:
            raise ValueError("timezone must be America/Chicago")
        for key in ("memory_root", "state_root", "token_path"):
            if not Path(data[key]).is_absolute():
                raise ValueError(f"{key} must be absolute")
        url = urlparse(data["ws_url"])
        if url.scheme not in {"ws", "wss"} or not url.hostname or not url.port:
            raise ValueError("explicit websocket endpoint required")
        if data.get("isolated") and (url.port == 7791 or url.hostname not in {"localhost", "127.0.0.1"}):
            raise ValueError("isolated endpoint must be an owned local test port")
        memory, state = Path(data["memory_root"]).resolve(), Path(data["state_root"]).resolve()
        if state == memory or memory in state.parents:
            raise ValueError("state must live outside shared memory")
        if data.get("sink"):
            raise ValueError("fixed sink bypass is unsupported; resolve the current binding")
        return cls(memory, state, data["ws_url"], Path(data["token_path"]), data["host"],
                   bool(data.get("isolated")), path)

    def rpc(self):
        return Config(self.ws_url, self.token_path.read_text().strip(), self.host,
                      self.state_root, self.memory_root)

    @property
    def namespace(self):
        return digest([self.host, str(self.state_root)])[:16]


def timer_due(stamp):
    if stamp.utcoffset() is None:
        raise ValueError("offset-aware timestamp required")
    return stamp.astimezone(ZONE).time() >= time(5)


def scan(settings):
    sources, gaps, duplicate, seen = {}, [], set(), set()
    for folder in ("completed", "deprecated"):
        root = settings.memory_root / "work" / folder
        if not root.is_dir():
            gaps.append({"path": str(root), "reason": "terminal directory unavailable"})
            continue
        try:
            paths = sorted(root.rglob("spec.md"))
        except OSError as exc:
            gaps.append({"path": str(root), "reason": str(exc)})
            continue
        for path in paths:
            relative = str(path.relative_to(settings.memory_root))
            try:
                meta = parse_frontmatter(path)
                identity = meta.get("id")
                if not identity or meta.get("status") not in {"completed", "deprecated"}:
                    raise ValueError("missing stable ID or nonterminal status")
                if identity in seen or identity in duplicate:
                    duplicate.add(identity)
                    sources.pop(identity, None)
                    raise ValueError(f"duplicate stable ID: {identity}")
                seen.add(identity)
                text = path.read_text().replace("\r\n", "\n")
                match = re.search(r"^##\s+Retro\b[^\n]*\n?(.*?)(?=^#{1,2}\s|\Z)", text,
                                  re.MULTILINE | re.DOTALL | re.IGNORECASE)
                if not match:
                    raise ValueError("missing Retro")
                original = match.group(0).strip()
                body = re.sub(r"^##\s+Retro\b\s*[:—-]?\s*", "", original, count=1, flags=re.IGNORECASE).strip()
                if not body:
                    raise ValueError("empty Retro")
                raw_day = meta.get(f"{meta['status']}_at") or meta.get("closed_at") or meta.get("updated_at")
                day = None
                if raw_day:
                    terminal = datetime.fromisoformat(str(raw_day).replace("Z", "+00:00"))
                    day = (terminal.astimezone(ZONE) if terminal.utcoffset() is not None else terminal).date().isoformat()
                sources[identity] = {"id": identity, "path": relative, "status": meta["status"],
                                     "terminal_date": day, "fingerprint": digest(original), "original": original}
            except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                gaps.append({"path": relative, "reason": str(exc)})
    return sources, gaps


def rebuild_index(settings):
    entries = {}
    manifests = sorted((settings.state_root / "runs").glob("*/collection.json"))
    for path in manifests:
        manifest = read(path)
        entries.update(manifest["baseline"])
        entries.update({s["id"]: {"fingerprint": s["fingerprint"], "reviewed": False,
                                 "enrolled_run": manifest["run_id"]} for s in manifest["sources"]})
    return {"initialized": bool(manifests), "entries": entries}


def collect(settings, stamp, *, max_sources=40, max_bytes=65536):
    if stamp.utcoffset() is None:
        raise ValueError("offset-aware cutoff required")
    local = stamp.astimezone(ZONE)
    run_id = local.date().isoformat()
    path = settings.state_root / "runs" / run_id / "collection.json"
    with locked(settings.state_root / "collect.lock"):
        index = rebuild_index(settings)
        if path.exists():
            atomic(settings.state_root / "index.json", index)
            return read(path)
        sources, gaps = scan(settings)
        baseline, selected, deferred, size = {}, [], [], 0
        previous = local.date() - timedelta(days=1)
        for identity, source in sorted(sources.items()):
            prior = index["entries"].get(identity)
            if prior and prior["fingerprint"] == source["fingerprint"]:
                continue
            if not index["initialized"] and (not source["terminal_date"] or source["terminal_date"] < previous.isoformat()):
                baseline[identity] = {"fingerprint": source["fingerprint"], "reviewed": False,
                                      "reason": "older or unknown-date initial history"}
                continue
            if source["terminal_date"] and source["terminal_date"] > local.date().isoformat():
                gaps.append({"path": source["path"], "reason": "future terminal date"})
                continue
            source_size = len(source["original"].encode())
            if len(selected) >= max_sources or size + source_size > max_bytes:
                deferred.append({k: source[k] for k in ("id", "path", "fingerprint")})
                continue
            selected.append(source)
            size += source_size
        start = datetime.combine(previous, time(), ZONE)
        end = datetime.combine(local.date(), time(), ZONE)
        manifest = {"run_id": run_id, "timezone": ZONE.key, "cutoff": stamp.isoformat(), "collected_at": now_iso(),
                    "window": {"start": start.isoformat(), "end": end.isoformat()},
                    "sources": selected, "baseline": baseline, "gaps": gaps, "deferred": deferred,
                    "coverage": {"readable_retros": len(sources), "selected": len(selected),
                                 "baseline_not_reviewed": len(baseline), "gaps": len(gaps), "deferred": len(deferred)}}
        atomic(path, manifest)  # This commits enrollment; index is a rebuildable projection.
        atomic(settings.state_root / "index.json", rebuild_index(settings))
        return manifest


def history_baseline(settings, baseline_path):
    """Prove the namespace and enrollment using reads only, before creating a lock."""
    baseline_path = Path(baseline_path).resolve()
    if (baseline_path.name != "collection.json" or baseline_path.parent.parent.name != "runs"
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", baseline_path.parent.name)):
        raise ValueError("daily baseline collection path required")
    daily_state = baseline_path.parents[2]
    state = settings.state_root.resolve()
    if state == daily_state or daily_state in state.parents or state in daily_state.parents:
        raise ValueError("history requires a separate state namespace")
    # A copied baseline path must not disguise a real daily namespace as history.
    for parent in (state, *state.parents):
        if (parent / "index.json").exists() or any(
                re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", p.parent.name)
                for p in (parent / "runs").glob("*/collection.json")):
            raise ValueError("history requires a separate state namespace")
    baseline = read(baseline_path)
    if (baseline.get("run_id") != baseline_path.parent.name or baseline.get("phase") == "history"
            or read(daily_state / "index.json") != rebuild_index(replace(settings, state_root=daily_state))):
        raise ValueError("daily baseline enrollment proof required")
    return baseline_path, baseline


def freeze_history(settings, baseline_path):
    """Snapshot an explicit daily baseline, without enrolling it in daily state."""
    baseline_path, baseline = history_baseline(settings, baseline_path)
    baseline_sha = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    path = settings.state_root / "history.json"
    inventory = read(path)
    if inventory is not None:
        if inventory.get("baseline_sha256") != baseline_sha:
            raise ValueError("historical baseline conflict")
        if inventory.get("inventory_digest") != digest({k: v for k, v in inventory.items() if k != "inventory_digest"}):
            raise ValueError("historical inventory digest mismatch")
        return inventory
    entries = baseline.get("baseline")
    if not isinstance(entries, dict) or not entries or set(entries) & {s["id"] for s in baseline["sources"]}:
        raise ValueError("explicit nonempty historical baseline required")
    current, _ = scan(settings)
    originals, batches, batch, size = [], [], [], 0
    for identity, entry in sorted(entries.items()):
        source = current.get(identity)
        if not source or entry.get("reviewed") is not False or source["fingerprint"] != entry.get("fingerprint"):
            raise ValueError(f"missing, malformed or changed historical original: {identity}")
        source_size = len(source["original"].encode())
        if source_size > 65536:
            raise ValueError(f"oversized historical original: {identity}")
        if len(batch) == 40 or size + source_size > 65536:
            batches.append(batch)
            batch, size = [], 0
        originals.append(source)
        batch.append(identity)
        size += source_size
    if batch:
        batches.append(batch)
    inventory = {"phase": "history", "baseline_path": str(baseline_path), "baseline_sha256": baseline_sha,
                 "collected_at": now_iso(), "sources": originals, "batches": batches}
    inventory["inventory_digest"] = digest(inventory)
    atomic(path, inventory)
    return inventory


def history_collect(settings, baseline_path, batch):
    history_baseline(settings, baseline_path)  # Refusal must not create paths in daily state.
    with locked(settings.state_root / "collect.lock"):
        inventory = freeze_history(settings, baseline_path)
        if type(batch) is not int or not 1 <= batch <= len(inventory["batches"]):
            raise ValueError("historical batch out of range")
        inventory_digest = inventory["inventory_digest"]
        originals = {s["id"]: s for s in inventory["sources"]}
        selected = [originals[identity] for identity in inventory["batches"][batch - 1]]
        run_id = f"history-{inventory_digest[:16]}-{batch:04}"
        manifest = {"run_id": run_id, "phase": "history", "timezone": ZONE.key, "window": None,
                    "cutoff": inventory["collected_at"], "collected_at": inventory["collected_at"],
                    "sources": selected, "baseline": {}, "gaps": [], "deferred": [],
                    "history": {"inventory_path": str(settings.state_root / "history.json"),
                                "inventory_digest": inventory_digest, "batch": batch,
                                "batches": len(inventory["batches"]), "originals": len(originals)},
                    "coverage": {"readable_retros": len(originals), "selected": len(selected),
                                 "baseline_not_reviewed": 0, "gaps": 0, "deferred": 0}}
        path = settings.state_root / "runs" / run_id / "collection.json"
        if path.exists():
            if read(path) != manifest:
                raise ValueError("edited or stale historical manifest")
        else:
            atomic(path, manifest)
        return manifest


def checked(response, kind):
    if response.get("type") != kind or response.get("ok") is False:
        raise RuntimeError(f"{kind}: {response.get('error_code') or response.get('reason') or response.get('type')}")
    return response


def validate_packet(packet, manifest):
    packet = json.loads(json.dumps(packet))  # Preserve the immutable raw worker report.
    if packet.get("run_id") != manifest["run_id"]:
        raise ValueError("packet run identity mismatch")
    selected = {s["id"]: s for s in manifest["sources"]}
    rows = packet.get("dispositions", [])
    if len(rows) != len(selected) or {r.get("id") for r in rows} != set(selected):
        raise ValueError("one disposition required for every original, including no-action")
    for row in rows:
        if row.get("fingerprint") != selected[row["id"]]["fingerprint"] or not row.get("reason"):
            raise ValueError("disposition must cite the retained original and give a reason")
    required = {"id", "problem", "consequence", "citations", "prior_occurrences", "existing", "action",
                "benefit", "effort", "risk", "uncertainty", "owner", "decision"}
    candidates = packet.get("candidates")
    if not isinstance(candidates, list) or len({c.get("id") for c in candidates}) != len(candidates):
        raise ValueError("unique candidate IDs required")
    for candidate in candidates:
        if required - candidate.keys() or not candidate["id"] or not candidate["citations"]:
            raise ValueError("incomplete recommendation")
        citations = candidate["citations"]
        if not isinstance(citations, list) or any(not isinstance(ref, str) for ref in citations):
            raise ValueError("citation IDs must be strings")
        originals = [ref for ref in citations if ref in selected]
        supplemental = [ref for ref in citations if ref not in selected]
        if not originals or any(ref.startswith("spec_") and ref not in packet.get("evidence_sources", {}) for ref in supplemental):
            raise ValueError("missing or fabricated collection original ID")
        if supplemental:
            candidate["citations"] = originals
            candidate["evidence_citations"] = list(dict.fromkeys(candidate.get("evidence_citations", []) + supplemental))
            packet.setdefault("normalization_notes", []).append({"candidate_id": candidate["id"], "evidence_refs": supplemental,
                "note": "Supplemental references retained as evidence labels; they do not expand original coverage."})
    return packet


def finding(candidate, alias, legacy=False):
    """Compile identity; model-provided keys never establish equivalence."""
    candidate = json.loads(json.dumps(candidate))
    attention = candidate.get("bart_attention")
    if attention is None and legacy:
        attention = {"reasons": ["uncertain"], "rationale": "Legacy packet requires current relevance review."}
    if (not isinstance(attention, dict) or not isinstance(attention.get("reasons"), list)
            or any(not isinstance(r, str) or r not in ATTENTION for r in attention["reasons"])
            or not isinstance(attention.get("rationale"), str) or not attention["rationale"].strip()):
        raise ValueError("invalid history attention annotation")
    attention = {"reasons": sorted(set(attention["reasons"])), "rationale": attention["rationale"]}
    candidate["bart_attention"] = attention
    key = candidate.get("finding_key", "legacy-" + digest(candidate["problem"])[:24])
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,119}", key):
        raise ValueError("invalid finding key")
    content = {k: v for k, v in candidate.items() if k not in {"id", "citations", "finding_key", "finding_version"}}
    # A changed occurrence must never disappear behind an earlier recommendation.
    if CHANGED & set(attention["reasons"]):
        content["occurrence"] = alias
    version = digest(content)
    candidate.update(finding_key=key, finding_version=version)
    return {"alias": alias, "candidate": candidate, "version": version}


def completed_history(settings, baseline_path):
    """Read and validate retained originals/reports before any admission or write."""
    history_baseline(settings, baseline_path)
    inventory = read(settings.state_root / "history.json")
    if not inventory or inventory.get("inventory_digest") != digest({k: v for k, v in inventory.items() if k != "inventory_digest"}):
        raise ValueError("valid frozen history inventory required")
    if inventory.get("baseline_sha256") != hashlib.sha256(Path(baseline_path).read_bytes()).hexdigest():
        raise ValueError("historical baseline conflict")
    originals = {s["id"]: s for s in inventory["sources"]}
    completed = {}
    for number, ids in enumerate(inventory["batches"], 1):
        run_id = f"history-{inventory['inventory_digest'][:16]}-{number:04}"
        root = settings.state_root / "runs" / run_id
        manifest = read(root / "collection.json")
        if manifest is None:
            continue
        if (manifest.get("run_id") != run_id or manifest.get("phase") != "history"
                or manifest.get("history", {}).get("inventory_digest") != inventory["inventory_digest"]
                or manifest["history"].get("batch") != number
                or manifest.get("sources") != [originals[i] for i in ids]):
            raise ValueError("edited or stale historical manifest")
        stages = [read(root / f"{name}.json", {}) for name in ("sol", "astra")]
        if not all(s.get("packet") for s in stages):
            continue
        for name, stage in zip(("sol", "astra"), stages):
            report = stage.get("report", {})
            model, effort = ("gpt-6-sol", "medium") if name == "sol" else ("gpt-6-astra", "high")
            if (stage.get("packet_hash") != digest(stage["packet"])
                    or report.get("effective_model") != model or report.get("effective_effort") != effort
                    or report.get("report_id") != stage.get("report_id")
                    or not stage.get("generation") or not stage.get("stream_id") or not stage.get("closed")
                    or not stage.get("cleanup", {}).get("session", {}).get("status") == "closed"
                    or stage["cleanup"]["session"].get("session_generation") != stage["generation"]):
                raise ValueError("history packet/report/model/hash/cleanup proof invalid")
            validate_packet(stage["packet"], manifest)
            inp = read(root / f"{name}-input-{stage.get('attempt')}.json")
            if not inp or inp.get("report_id") != stage["report_id"] or inp["collection"].get("sources") != manifest["sources"]:
                raise ValueError("history report input provenance invalid")
            validator = validate_history_packet if inp["collection"].get("cumulative_context") else validate_packet
            expected = validator(report.get("extras", {}).get("daily_retro", {}), manifest)
            expected["collection"] = inp["collection"]
            if expected != stage["packet"]:
                raise ValueError("history packet differs from retained worker report")
        mode = read(root / "history-mode.json")
        review = read(root / "review.json")
        if mode is None and review is None:
            raise ValueError("unreviewed legacy history is not a consolidation input")
        if mode is not None and mode.get("no_deliver") is not True:
            raise ValueError("unknown history delivery mode")
        if mode:
            context_path = root / "history-context.json"
            if (mode.get("context_path") != str(context_path) or not context_path.exists()
                    or hashlib.sha256(context_path.read_bytes()).hexdigest() != mode.get("context_sha256")):
                raise ValueError("edited cumulative context")
        if review and review.get("result", {}).get("packet_hash") != stages[1]["packet_hash"]:
            raise ValueError("history review hash mismatch")
        completed[number] = {"manifest": manifest, "final": stages[1], "review": review,
                             "mode": mode, "root": str(root)}
    return inventory, completed


def history_checkpoints(settings):
    for path in sorted((settings.state_root / "runs").glob("history-consolidated-*/collection.json")):
        manifest = read(path)
        if manifest.get("consolidation", {}).get("mode") == "checkpoint":
            yield path.parent, manifest


def history_catalogue(completed):
    entries = []
    for number, run in sorted(completed.items()):
        stage = run["final"]
        for candidate in stage["packet"]["candidates"]:
            alias = {"run_id": run["manifest"]["run_id"], "candidate_id": candidate["id"], "packet_hash": stage["packet_hash"]}
            row = finding(candidate, alias, legacy=run["mode"] is None)
            row["alias_id"] = digest(alias)
            row["prior_review"] = next((r for r in (run["review"] or {}).get("result", {}).get("dispositions", [])
                                       if r["id"] == candidate["id"]), None)
            entries.append(row)
    return entries


def history_context(settings, inventory, completed):
    entries = history_catalogue(completed)
    checkpoints = []
    for root, manifest in history_checkpoints(settings):
        final = read(root / "astra.json")
        if not final or final.get("packet_hash") != digest(final["packet"]):
            raise ValueError("incomplete or edited prior checkpoint")
        review = read(root / "review.json")
        if review and review.get("result", {}).get("packet_hash") != final["packet_hash"]:
            raise ValueError("checkpoint review hash mismatch")
        checkpoints.append({"run_id": manifest["run_id"], "packet_hash": final["packet_hash"],
                            "review": review, "quiet": not final["packet"]["candidates"]})
    return {"inventory_digest": inventory["inventory_digest"], "findings": entries,
            "inputs": [{"run_id": r["manifest"]["run_id"], "packet_hash": r["final"]["packet_hash"],
                        "packet_path": str(Path(r["root"]) / "astra.json"), "review": r["review"]}
                       for _, r in sorted(completed.items())], "checkpoints": checkpoints,
            "work_root": str(settings.memory_root / "work")}


def validate_history_packet(packet, manifest):
    packet = validate_packet(packet, manifest)
    for c in packet["candidates"]:
        compiled = finding(c, {"run_id": manifest["run_id"], "candidate_id": c["id"]})
        c.update(compiled["candidate"])
    return packet


def validate_final_selection(packet, manifest):
    catalogue = manifest["consolidation"]["catalogue"]
    if packet.get("run_id") != manifest["run_id"] or packet.get("input_digest") != digest(catalogue):
        raise ValueError("final selection identity/input digest mismatch")
    rows = packet.get("selections")
    aliases = {r["alias_id"]: r for r in catalogue}
    if (not isinstance(rows, list) or len(rows) != len(aliases)
            or {r.get("alias_id") for r in rows} != set(aliases)):
        raise ValueError("one final selection per retained alias required")
    packet = json.loads(json.dumps(packet))
    for row in packet["selections"]:
        if row.get("disposition") not in {"keep", "drop"} or not row.get("reason"):
            raise ValueError("final keep/drop reason required")
        candidate = json.loads(json.dumps(row.get("candidate", aliases[row["alias_id"]]["candidate"])))
        if "bart_attention" in row:
            candidate["bart_attention"] = row["bart_attention"]
        original = aliases[row["alias_id"]]["candidate"]
        if (candidate.get("id") != original["id"] or candidate.get("citations") != original["citations"]
                or candidate.get("evidence_citations", []) != original.get("evidence_citations", [])):
            raise ValueError("final selection invented alias or citation")
        checked_candidate = finding(candidate, aliases[row["alias_id"]]["alias"])["candidate"]
        if row["disposition"] == "drop" and checked_candidate["bart_attention"]["reasons"]:
            raise ValueError("cannot drop a relevant or uncertain final finding")
        if row["disposition"] == "keep" and not checked_candidate["bart_attention"]["reasons"]:
            raise ValueError("final keep requires relevance rationale")
        row["candidate"] = checked_candidate
    return packet


def compile_history_packet(manifest, completed, selections=None):
    catalogue = manifest["consolidation"]["catalogue"]
    selected = {r["alias_id"]: r for r in selections["selections"]} if selections else {}
    already = set(manifest["consolidation"]["already_reported"])
    groups, audit = {}, []
    for entry in catalogue:
        row = selected.get(entry["alias_id"])
        candidate = row["candidate"] if row else entry["candidate"]
        version = finding(candidate, entry["alias"])["version"]
        eligible = bool(candidate["bart_attention"]["reasons"])
        include = row["disposition"] == "keep" if row else eligible and version not in already
        audit.append({**entry, "selected": include, "final_selection": row,
                      "reason": row["reason"] if row else candidate["bart_attention"]["rationale"]})
        if include:
            group = groups.setdefault(version, {**candidate, "id": version, "finding_version": version,
                                               "citations": [], "aliases": [], "prior_reviews": []})
            group["citations"] = sorted(set(group["citations"] + candidate["citations"]))
            group["aliases"].append(entry["alias"])
            if entry["prior_review"]:
                group["prior_reviews"].append(entry["prior_review"])
            for checkpoint in manifest["consolidation"]["context"]["checkpoints"]:
                for review in (checkpoint["review"] or {}).get("result", {}).get("dispositions", []):
                    if review["id"] == entry["version"] and review not in group["prior_reviews"]:
                        group["prior_reviews"].append(review)
    dispositions, evidence = [], {}
    for _, run in sorted(completed.items()):
        packet = run["final"]["packet"]
        dispositions.extend(packet["dispositions"])
        sources = packet.get("evidence_sources", {})
        if isinstance(sources, list):
            sources = ((value.get("id") or f"evidence-{index}", value)
                       if isinstance(value, dict) else (f"evidence-{index}", value)
                       for index, value in enumerate(sources))
        else:
            sources = sources.items()
        for key, value in sources:
            if key in evidence and evidence[key] != value:
                # Labels are batch-local; retain conflicting proofs without rewriting citations.
                qualified = f"{run['manifest']['run_id']}:{key}:{digest(value)}"
                key, suffix = qualified, 1
                while key in evidence and evidence[key] != value:
                    key = f"{qualified}:{suffix}"
                    suffix += 1
            evidence[key] = value
    packet = {"run_id": manifest["run_id"], "dispositions": dispositions,
              "candidates": [groups[k] for k in sorted(groups)], "evidence_sources": evidence,
              "audit": audit, "counts": {"originals": len(dispositions), "candidate_occurrences": len(catalogue),
                  "surfaced_occurrences": sum(r["selected"] for r in audit), "surfaced_candidates": len(groups),
                  "retained_only_candidates": sum(not r["selected"] for r in audit)}}
    return validate_packet(packet, manifest)


def worker_collection(manifest, path):
    """Keep historical enrollment bookkeeping out of the analytical input."""
    if manifest.get("consolidation"):
        return {"run_id": manifest["run_id"], "phase": "history-final", "manifest_path": str(path),
                "sources_count": len(manifest["sources"]), "consolidation": manifest["consolidation"]}
    return {**{k: v for k, v in manifest.items() if k != "baseline"},
            "baseline_exclusions": {"count": len(manifest["baseline"]), "reviewed": False,
                                    "path": str(path), "manifest_hash": digest(manifest)}}


def worker_prompt(settings, manifest, stage, input_path):
    model, effort = ("gpt-6-sol", "medium") if stage == "sol" else ("gpt-6-astra", "high")
    duty = ("Investigate originals, named follow-ups, related current work/rules and prior decisions; group repeated issues. "
            "Verify current defects rather than treating retros as conclusions. Draft one prioritized packet.") if stage == "sol" else (
            "Read EVERY original and ALL Sol dispositions/draft, including no-action. Detect omitted insights and evidence gaps. "
            "Correct small gaps directly, flag substantial uncertainty without returning to Sol, finalize other decisions.")
    report_id = read(input_path)["report_id"]
    if stage == "final-astra":
        return f"""Final historical relevance/dedupe review; Codex gpt-6-astra/high, READ ONLY.
Read immutable input JSON {input_path}; memory root {settings.memory_root}. This is a findings review, not a repeated original investigation. Read the complete catalogue in collection.consolidation and prior reviews. Verify current relevant work/decisions; retained per-batch paths/hashes give every original/disposition for targeted verification. No edits, questions, new lanes, delivery or improvement execution. One report self-closes your generation.
For every alias_id return exactly one keep/drop selection with reason and current bart_attention. The original candidate is retained by reference; include a full candidate override ONLY when action, current existing/owner/decision or evidence requires correction. Retain all citation IDs/evidence_citations and candidate id. Drop deprecated, retired, shipped/resolved or already-owned unchanged subjects ONLY with verified evidence. KEEP recurrence, changed_evidence, ownership_gap, revised_action, new_grant, no_owning_work or uncertain cases even on linked/shipped work. An existing work link or triage backlog alone cannot suppress. Empty bart_attention.reasons requires a verified unchanged/excluded subject; include rationale. Full audit preserves dropped findings. Finding versions are computed by producer; do not invent hashes.
Return agent-orch report --msg-id 0 --status done --report-id {report_id} --result JSON (ReportPayloadV1 summary/findings/next_action/extras). extras.daily_retro={{run_id:"{manifest['run_id']}",input_digest:"{digest(manifest['consolidation']['catalogue'])}",selections:[{{alias_id,disposition:"keep"|"drop",reason,bart_attention,candidate(optional override)}}]}}. No omitted/invented aliases. Prior checkpoint actions are historical custody, never a new commission. Preserve recurrence/changed evidence and genuine uncertainty.
"""
    history_duty = ""
    if manifest.get("cumulative_context"):
        context = manifest["cumulative_context"]
        history_duty = f"""\nHistorical serial context: read {context['path']} (SHA256 {context['sha256']}); all prior findings, source hashes, reviews and work references are retained there. Read every new original for coverage, but skip renewed detailed investigation of an equivalent unchanged known finding; disposition cites prior finding key/version and current original. A new recurrence/evidence/owner/action/grant is a new version and must be evaluated. A known issue alone is not proof of deprecation. No silent truncation.
Each candidate adds finding_key (stable lowercase issue label <=120 chars) and bart_attention={{reasons:[no_owning_work|new_grant|recurrence|changed_evidence|ownership_gap|revised_action|uncertain],rationale}}. Producer computes finding_version. Drop deprecated/retired/shipped/resolved/currently accepted owned unchanged subjects by empty reasons ONLY with current evidence in existing/owner/action/decision/rationale. Keep every exception even on existing work; unassigned triage is not an executing owner. Ambiguous relevance remains uncertain. Existing-work link alone never suppresses. Keep all candidates/full source dispositions, including unchanged exclusions, in this private packet; producer selects checkpoint relevance.\n"""
    return f"""Daily retrospective {stage} pass, run {manifest['run_id']}; {model}/{effort}, Codex only.
{duty}
Read immutable input JSON {input_path}; memory root {settings.memory_root}. Look up only relevant normal work records and bounded recent packets under {settings.state_root}/runs for recurrence/prior decisions. Respect existing standing grants.{history_duty}
READ ONLY: no edits, questions, publication, new lanes or feedback pass. No authority to execute improvements. Your terminal report self-closes this generation; producer owns only fenced cleanup.
Return one durable report: agent-orch report --msg-id 0 --status done --report-id {report_id} --result JSON. ReportPayloadV1 summary/findings/next_action/extras; extras.daily_retro is the packet object.
Packet: run_id; dispositions [{{id,fingerprint,reason}}] EXACTLY once for each original, including no-action; candidates unique prioritized [{{id,problem,consequence,citations:[source IDs],prior_occurrences,existing,action,benefit,effort,risk,uncertainty,owner,decision}}]. decision describes exact needed grant/recommended option/meaningful alternatives, or existing authorization/no decision. Candidates.citations contain ONLY IDs from collection.sources; put verification labels, file paths and related-work references in evidence_sources/evidence_citations or existing, never in original citations. Each recommendation needs at least one collected original ID. Existing fields name authoritative fixes/rules/work and current state. No-new sources is an explicit successful empty packet; coverage gaps never imply all-clear. Preserve prior materially changed recommendations/actions needing attention. Keep front concise, no silent source truncation.
"""


class Pipeline:
    def __init__(self, settings, rpc=wsclient):
        self.settings, self.rpc, self.config = settings, rpc, settings.rpc()

    async def binding(self):
        binding = checked(await self.rpc.assistant_once(self.config, {"type": "assistant.binding"}), "assistant.binding.ok")
        binding["session_generation"] = binding.get("generation") or binding.get("session_generation")
        if not binding.get("stream_id") or not binding.get("session_generation"):
            raise RuntimeError("current assistant binding unavailable")
        return binding

    async def worker(self, manifest, name, prior=None):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        receipt_path = root / f"{name}.json"
        stage = read(receipt_path, {})
        if stage.get("packet"):
            return stage
        model, effort = ("gpt-6-sol", "medium") if name == "sol" else ("gpt-6-astra", "high")
        if not stage:
            attempt = 1
        elif stage.get("failed"):
            attempt = stage["attempt"] + 1
            if attempt > 2:
                raise RuntimeError(f"{name} recovery budget exhausted; retained for assistant")
            history = read(root / f"{name}-attempts.json", [])
            history.append(stage)
            atomic(root / f"{name}-attempts.json", history)
            stage = {}
        else:
            attempt = stage["attempt"]
        if not stage:
            input_path = root / f"{name}-input-{attempt}.json"
            report_id = f"daily-retro-{self.settings.namespace}-{name}-{manifest['run_id']}-{attempt}"
            atomic(input_path, {"collection": worker_collection(manifest, root / "collection.json"),
                                "sol": prior, "report_id": report_id})
            key = f"daily-retro-{self.settings.namespace}-{manifest['run_id']}-{name}-{attempt}"
            stage = {"attempt": attempt, "created_at": now_iso(), "report_id": report_id,
                     "payload": {"host": self.settings.host, "provider": "codex", "model": model,
                                 "effort": effort, "visibility": "hidden", "role": "worker", "cwd": str(self.settings.memory_root),
                                 "self_close_on_completion": True,
                                 "request_id": key, "idempotency_key": key,
                                 "initial_prompt": worker_prompt(self.settings, manifest, name, input_path)}}
            atomic(receipt_path, stage)  # Intent survives interruption before/after admission.
        if not stage.get("stream_id"):
            # Repeating the exact spawn key is the existing admission reconciliation contract.
            admitted = await self.rpc.spawn_once(self.config, dict(stage["payload"]))
            row = admitted.get("session") or {}
            stage["admission"] = admitted
            stage["stream_id"] = admitted.get("stream_id") or row.get("stream_id")
            stage["generation"] = row.get("session_generation") or admitted.get("session_generation")
            atomic(receipt_path, stage)
            if admitted.get("type") != "spawn.ok" or not stage["stream_id"] or not stage["generation"]:
                raise RuntimeError(f"{name} admission indeterminate; exact intent retained")
        if not stage.get("generation"):
            raise RuntimeError("owned generation unproven; cleanup and new admission blocked")
        deadline = monotonic() + 3600
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError("worker report deadline exceeded")
            try:
                response = await asyncio.wait_for(
                    self.rpc.await_report_once(self.config, stage["stream_id"], 0,
                                               include_details=True, include_extras=True,
                                               timeout=min(900, remaining)), timeout=remaining)
            except TimeoutError as exc:
                raise RuntimeError("worker report deadline exceeded") from exc
            if response.get("type") != "await_report.timeout":
                break
            if response.get("stream_id") != stage["stream_id"] or response.get("msg_id") != 0:
                checked(response, "await_report.ok")
        if response.get("result_kind") == "closed_without_report" or (response.get("result_kind") == "report" and not response.get("ok")):
            stage.update(failed=True, failure=response)
            atomic(receipt_path, stage)
            raise RuntimeError(f"{name} confirmed failed; recover on next run")
        checked(response, "await_report.ok")
        report = response["report"]
        if report.get("effective_model") != model or report.get("effective_effort") != effort:
            raise RuntimeError("worker report model/effort mismatch")
        if report.get("report_id") != stage["report_id"]:
            raise RuntimeError("unexpected worker report identity")
        validator = (validate_final_selection if name == "final-astra" else
                     validate_history_packet if manifest.get("cumulative_context") else validate_packet)
        try:
            packet = validator(report.get("extras", {}).get("daily_retro", {}), manifest)
        except Exception:
            stage.update(failed=True, failure="invalid terminal packet", report=report)
            atomic(receipt_path, stage)
            raise
        packet = {**packet, "collection": worker_collection(manifest, root / "collection.json")}
        stage.update(packet=packet, packet_hash=digest(packet), report=report, completed_at=now_iso())
        atomic(receipt_path, stage)
        return stage

    async def deliver(self, manifest, final=None, failure=None):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        path = root / ("failure-delivery.json" if failure else "delivery.json")
        record = read(path, {"attempts": []})
        if (root / "review.json").exists():
            return record
        binding = await self.binding()
        target, generation = binding["stream_id"], binding["session_generation"]
        attempt = next((a for a in record["attempts"] if a["target"] == target and a["generation"] == generation), None)
        if attempt and attempt.get("confirmed"):
            return record
        if not attempt:
            key = "daily-retro-" + digest([self.settings.namespace, manifest["run_id"], target, generation, bool(failure)])[:32]
            if failure:
                body = f"REPORT daily-retro failure {manifest['run_id']}: {failure}. Retained state: {root}. Retry with existing producer; no operator notification for routine retries."
            else:
                summary = ""
                if manifest.get("consolidation"):
                    packet = final["packet"]
                    summary = (f" mode={manifest['consolidation']['mode']} counts={encoded(packet['counts']).decode()} "
                               f"relevant={encoded([{'id': c['id'], 'problem': c['problem'], 'action': c['action'], 'attention': c['bart_attention'], 'citations': c['citations'], 'prior_reviews': c.get('prior_reviews', [])} for c in packet['candidates']]).decode()}. "
                               "Previously reviewed versions retain their work/decision custody; do not recommission them. ")
                body = (f"REPORT daily-retro ready run={manifest['run_id']} report_id={final['report']['report_id']} "
                        f"packet_hash={final['packet_hash']} path={root / 'astra.json'}. "
                        f"{summary}"
                        f"Ingest once and review now under the accepted daily retro contract. Record every disposition with "
                        f"{Path(__file__).resolve()} record-review --config {self.settings.config_path} --run-id {manifest['run_id']} --result RESULT_JSON. "
                        "Use normal work proposal records and decision helper to recover still-open decisions after generation replacement. "
                        "Quiet/no-action days: retain review receipt, emit no chat prose or questions. Surface only real decisions via durable prompt ask. No assistant.publish obligation.")
            host, session = target.split(":", 1)
            attempt = {"target": target, "generation": generation, "request_id": key,
                       "payload": {"host": host, "session_name": session, "text": body,
                                   "request_id": key, "optimistic_id": key}}
            record["attempts"].append(attempt)
            atomic(path, record)
        receipts = await self.rpc.send_receipt_once(self.config, target, attempt["request_id"])
        if receipts.get("type") != "send.receipt.get.ok":
            raise RuntimeError("delivery reconciliation unavailable")
        landed = next((r for r in receipts.get("receipts", []) if r.get("delivery") == "landed" or r.get("state") == "landed"), None)
        response = landed or await self.rpc.send_once(self.config, dict(attempt["payload"]))
        attempt["receipt"] = response
        attempt["confirmed"] = response.get("delivery") == "landed" or response.get("state") == "landed"
        attempt["at"] = now_iso()
        attempt["collection_to_delivery_seconds"] = (datetime.fromisoformat(attempt["at"]) - datetime.fromisoformat(manifest["collected_at"])).total_seconds()
        atomic(path, record)
        if not attempt["confirmed"]:
            raise RuntimeError("delivery pending; exact target/body/key retained")
        return record

    async def cleanup(self, manifest, force=False):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        for name in STAGES:
            path = root / f"{name}.json"
            stage = read(path, {})
            if stage.get("closed") or not stage.get("generation") or not (force or stage.get("packet") or stage.get("failed")):
                continue
            result = await self.rpc.close_once(self.config, stage["stream_id"],
                                               expected_generation=stage["generation"], reason="report_terminate")
            stage["cleanup"] = result
            row = result.get("session") or {}
            stage["closed"] = (result.get("type") in {"close.ok", "close.already_closed"}
                               and not result.get("failed") and row.get("status") == "closed"
                               and row.get("session_generation") == stage["generation"])
            atomic(path, stage)
            if not stage["closed"]:
                raise RuntimeError("generation-owned worker cleanup blocked")

    async def run(self, stamp=None, on_demand=False):
        stamp = stamp or datetime.now(timezone.utc)
        if not on_demand and not timer_due(stamp):
            return {"state": "before_05_local"}
        with locked(self.settings.state_root / "run.lock"):
            collect(self.settings, stamp)
            completed = []
            for path in sorted((self.settings.state_root / "runs").glob("*/collection.json")):
                manifest = read(path)
                if await self.run_manifest(manifest):
                    completed.append(manifest["run_id"])
            return {"delivered": completed}

    async def run_manifest(self, manifest):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        if (root / "review.json").exists():
            await self.cleanup(manifest)
            return False
        try:
            sol = await self.worker(manifest, "sol")
            astra = await self.worker(manifest, "astra", sol["packet"])
            await self.deliver(manifest, astra)
            return True
        except Exception as exc:
            atomic(root / "failure.json", {"at": now_iso(), "error": str(exc)})
            try:
                await self.deliver(manifest, failure=str(exc))
            except Exception as delivery_error:
                atomic(root / "failure-notice-error.json", {"error": str(delivery_error)})
            raise
        finally:
            await self.cleanup(manifest)

    async def history_run(self, baseline_path, batch, no_deliver=False):
        history_baseline(self.settings, baseline_path)
        if no_deliver and (self.settings.state_root / "history.json").exists():
            completed_history(self.settings, baseline_path)
        with locked(self.settings.state_root / "run.lock"):
            manifest = history_collect(self.settings, baseline_path, batch)
            root = self.settings.state_root / "runs" / manifest["run_id"]
            mode = read(root / "history-mode.json")
            if mode and not no_deliver:
                raise ValueError("history delivery mode conflict")
            if no_deliver:
                inventory, completed = completed_history(self.settings, baseline_path)
                if batch in completed:
                    return {"prepared": [] if completed[batch]["review"] else [manifest["run_id"]], "delivered": []}
                if any(n not in completed for n in range(1, batch)):
                    raise ValueError("serial history requires preceding batches completed")
                if any(read(root / f"{stage}.json") for stage in STAGES) and mode is None:
                    raise ValueError("history delivery mode conflict")
                if mode is None:
                    context = history_context(self.settings, inventory, completed)
                    context_path = root / "history-context.json"
                    atomic(context_path, context)
                    mode = {"no_deliver": True, "context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
                            "context_path": str(context_path)}
                    atomic(root / "history-mode.json", mode)
                context_path = root / "history-context.json"
                if (mode.get("no_deliver") is not True or mode.get("context_path") != str(context_path)
                        or not context_path.exists() or hashlib.sha256(context_path.read_bytes()).hexdigest() != mode.get("context_sha256")):
                    raise ValueError("edited cumulative context")
                for prior in read(context_path)["inputs"]:
                    packet = read(Path(prior["packet_path"]))
                    if not packet or packet.get("packet_hash") != prior["packet_hash"] or digest(packet["packet"]) != prior["packet_hash"]:
                        raise ValueError("edited cumulative packet provenance")
                manifest = {**manifest, "cumulative_context": {"path": str(context_path), "sha256": mode["context_sha256"]}}
                if any(read(root / f"{name}.json", {}).get("failed") for name in STAGES):
                    raise RuntimeError("historical worker recovery requires parent ruling")
                try:
                    sol = await self.worker(manifest, "sol")
                    await self.worker(manifest, "astra", sol["packet"])
                except Exception as exc:
                    atomic(root / "failure.json", {"at": now_iso(), "error": str(exc)})
                    raise
                finally:
                    await self.cleanup(manifest, force=True)
                completed_history(self.settings, baseline_path)
                return {"prepared": [manifest["run_id"]], "delivered": []}
            delivered = await self.run_manifest(manifest)
            return {"delivered": [manifest["run_id"]] if delivered else []}

    async def history_consolidate(self, baseline_path, batches=None, final=False):
        inventory, completed = completed_history(self.settings, baseline_path)
        if final:
            if batches is not None or set(completed) != set(range(1, len(inventory["batches"]) + 1)):
                raise ValueError("final consolidation requires complete history inventory")
            numbers = sorted(completed)
        else:
            if (not isinstance(batches, list) or not 1 <= len(batches) <= 5
                    or any(type(n) is not int for n in batches) or len(set(batches)) != len(batches)):
                raise ValueError("one to five unique batch numbers required")
            numbers = sorted(batches)
            if any(n not in completed or completed[n]["mode"] is None or completed[n]["review"] for n in numbers):
                raise ValueError("checkpoint requires completed no-deliver batches")
        chosen = {n: completed[n] for n in numbers}
        members = [{"run_id": r["manifest"]["run_id"], "packet_hash": r["final"]["packet_hash"]} for _, r in sorted(chosen.items())]
        mode = "final" if final else "checkpoint"
        run_id = "history-consolidated-" + digest([self.settings.namespace, inventory["inventory_digest"], mode, members])
        root = self.settings.state_root / "runs" / run_id
        with locked(self.settings.state_root / "run.lock"):
            existing = read(root / "collection.json")
            reported = set()
            for prior_root, prior_manifest in history_checkpoints(self.settings):
                if prior_manifest["run_id"] == run_id:
                    continue
                if not final and set(numbers) & set(prior_manifest["consolidation"]["batches"]):
                    raise ValueError("overlapping history checkpoint")
                stage = read(prior_root / "astra.json")
                if not stage or stage.get("packet_hash") != digest(stage["packet"]):
                    raise ValueError("incomplete or edited prior checkpoint")
                delivery = read(prior_root / "delivery.json", {"attempts": []})
                if stage["packet"]["candidates"] and not any(a.get("confirmed") for a in delivery["attempts"]):
                    raise RuntimeError("prior checkpoint delivery unresolved")
                reported.update(c["finding_version"] for c in stage["packet"]["candidates"])
            if existing:
                manifest = existing
                expected_catalogue = history_catalogue(chosen)
                catalogue = manifest.get("consolidation", {}).get("catalogue", [])
                if (manifest.get("run_id") != run_id or manifest.get("phase") != "history-consolidated"
                        or manifest["consolidation"].get("members") != members
                        or manifest["consolidation"].get("mode") != mode
                        or manifest["consolidation"].get("batches") != numbers
                        or manifest.get("sources") != [s for _, r in sorted(chosen.items()) for s in r["manifest"]["sources"]]
                        or [{k: v for k, v in row.items() if k != "prior_review"} for row in catalogue] !=
                           [{k: v for k, v in row.items() if k != "prior_review"} for row in expected_catalogue]):
                    raise ValueError("edited consolidation membership")
            else:
                context = history_context(self.settings, inventory, completed)
                manifest = {"run_id": run_id, "phase": "history-consolidated", "baseline": {},
                            "collected_at": inventory["collected_at"],
                            "sources": [s for _, r in sorted(chosen.items()) for s in r["manifest"]["sources"]],
                            "consolidation": {"mode": mode, "batches": numbers, "members": members,
                                "catalogue": history_catalogue(chosen), "already_reported": sorted(reported),
                                "context": context, "context_digest": digest(context)}}
                atomic(root / "collection.json", manifest)
            if digest(manifest["consolidation"]["context"]) != manifest["consolidation"]["context_digest"]:
                raise ValueError("edited consolidation context")
            compiled = read(root / "astra.json")
            if compiled:
                if compiled.get("packet_hash") != digest(compiled["packet"]):
                    raise ValueError("edited compiled history packet")
                validate_packet(compiled["packet"], manifest)
                selection = None
                if final:
                    retained = read(root / "final-astra.json", {})
                    report = retained.get("report", {})
                    if (report.get("report_id") != retained.get("report_id")
                            or report.get("effective_model") != "gpt-6-astra" or report.get("effective_effort") != "high"
                            or retained.get("packet_hash") != digest(retained.get("packet"))):
                        raise ValueError("final report/model/hash proof invalid")
                    selection = validate_final_selection(report.get("extras", {}).get("daily_retro", {}), manifest)
                    selection["collection"] = worker_collection(manifest, root / "collection.json")
                    if selection != retained["packet"] or compiled["report"] != report:
                        raise ValueError("compiled final differs from retained worker report")
                if compiled["packet"] != compile_history_packet(manifest, chosen, selection):
                    raise ValueError("compiled packet differs from immutable inputs")
            else:
                selection = None
                try:
                    if final:
                        if read(root / "final-astra.json", {}).get("failed"):
                            raise RuntimeError("final worker recovery requires parent ruling")
                        selection = await self.worker(manifest, "final-astra")
                    packet = compile_history_packet(manifest, chosen, selection["packet"] if selection else None)
                    compiled = {"packet": packet, "packet_hash": digest(packet), "producer": "history-consolidation",
                                "report": selection["report"] if selection else {
                                    "report_id": "history-checkpoint-" + digest(members), "kind": "deterministic-compilation",
                                    "source_report_ids": [r["final"]["report_id"] for _, r in sorted(chosen.items())]}}
                    atomic(root / "astra.json", compiled)
                finally:
                    await self.cleanup(manifest, force=True)
            # A compiled receipt may survive interruption before owned cleanup.
            await self.cleanup(manifest, force=True)
            if final or compiled["packet"]["candidates"]:
                await self.deliver(manifest, compiled)
            return {"run_id": run_id, "packet_hash": compiled["packet_hash"], "counts": compiled["packet"]["counts"],
                    "quiet": not final and not compiled["packet"]["candidates"]}

    async def actor(self):
        binding = await self.binding()
        actor = os.environ.get("PENTACLE_STREAM_ID") or os.environ.get("AGENT_ORCH_STREAM_ID")
        if actor != binding["stream_id"]:
            raise RuntimeError("only CURRENT assistant may ingest or prepare decisions")
        inspected = checked(await self.rpc.inspect_stream_once(self.config, actor), "inspect_stream.ok")
        row = inspected.get("session") or {}
        if row.get("status") != "open" or row.get("session_generation") != binding["session_generation"]:
            raise RuntimeError("open current assistant generation required")
        return binding

    async def record_review(self, run_id, result):
        binding = await self.actor()
        if not re.fullmatch(r"(?:\d{4}-\d{2}-\d{2}|history-[0-9a-f]{16}-\d{4}|history-consolidated-[0-9a-f]{64})", run_id):
            raise ValueError("invalid run ID")
        root = self.settings.state_root / "runs" / run_id
        with locked(root / "review.lock"):
            final = read(root / "astra.json")
            if not final or result.get("packet_hash") != final.get("packet_hash"):
                raise ValueError("review must bind exact final packet hash")
            rows = result.get("dispositions", [])
            candidates = {c["id"] for c in final["packet"]["candidates"]}
            if len(rows) != len(candidates) or {r.get("id") for r in rows} != candidates:
                raise ValueError("one assistant disposition per recommendation required")
            for row in rows:
                if row.get("disposition") not in DISPOSITIONS or not row.get("reason"):
                    raise ValueError("explicit assistant disposition/reason required")
                if row["disposition"] in {"investigate", "authorized", "propose", "defer"}:
                    path = work_path(self.settings, row.get("work_id"))
                    records = proposals(path.read_text())
                    proposal = records.get(row.get("proposal_id"))
                    if not proposal or proposal.get("version") != row.get("version") or not proposal.get("citations"):
                        raise ValueError("action needs durable proposal/version/citations in normal work")
            old = read(root / "review.json")
            if old:
                if old["result"] != result:
                    raise ValueError("run already reviewed; amend normal work rather than repeat ingestion")
                return old
            receipt = {"result": result, "actor": binding, "reviewed_at": now_iso(), "decision_ready_at": now_iso()}
            manifest = read(root / "collection.json")
            receipt["collection_to_decision_ready_seconds"] = (datetime.fromisoformat(receipt["decision_ready_at"]) - datetime.fromisoformat(manifest["collected_at"])).total_seconds()
            atomic(root / "review.json", receipt)
            return receipt

    async def _blocked_report(self, path, preimage, records, record, attempt):
        notice = attempt.get("blocked_report")
        if notice is None:
            target = attempt["producer"]
            host, session = target.split(":", 1)
            key = "retro-blocked-" + digest([self.settings.namespace, str(path), attempt["question_id"]])[:32]
            notice = {"target": target, "request_id": key, "payload": {
                "host": host, "session_name": session, "request_id": key, "optimistic_id": key,
                "text": f"REPORT daily-retro decision blocked proposal={record['id']} version={record['version']} state=ask_blocked reason=ask_parent path={path}. No authorization or retry loop; after the current-assistant prompt admission is live, resume explicitly with decision --retry-blocked."}}
            attempt["blocked_report"] = notice
        records[record["id"]] = record
        preimage = save_proposals(path, preimage, records)
        if notice.get("confirmed"):
            return record
        receipts = checked(await self.rpc.send_receipt_once(self.config, notice["target"], notice["request_id"]), "send.receipt.get.ok")
        landed = next((r for r in receipts.get("receipts", []) if r.get("delivery") == "landed" or r.get("state") == "landed"), None)
        response = landed or await self.rpc.send_once(self.config, dict(notice["payload"]))
        notice.update(receipt=response, confirmed=response.get("delivery") == "landed" or response.get("state") == "landed")
        save_proposals(path, preimage, records)
        if not notice["confirmed"]:
            raise RuntimeError("blocked decision REPORT pending; exact notice retained")
        return record

    async def decision(self, work_id, proposal, *, retry_blocked=False):
        binding = await self.actor()
        path = work_path(self.settings, work_id)
        identity = proposal.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", identity):
            raise ValueError("stable proposal ID required")
        for field in ("scope", "citations", "owner", "checkpoint", "success_measure"):
            if not proposal.get(field):
                raise ValueError(f"proposal requires {field}")
        disposition = proposal.get("disposition", "propose")
        if disposition not in DISPOSITIONS:
            raise ValueError("unknown proposal disposition")
        if disposition == "propose":
            for field in ("title", "body", "options"):
                if not proposal.get(field):
                    raise ValueError(f"operator decision requires {field}")
        if disposition in {"authorized", "investigate"} and not proposal.get("authority"):
            raise ValueError("existing authority reference required")
        version = digest({k: proposal.get(k) for k in ("scope", "title", "body", "options")})
        with locked(self.settings.state_root / "locks" / f"{work_id}.lock"):
            preimage = path.read_bytes()
            text = preimage.decode()
            records = proposals(text)
            record = records.get(identity, {"attempts": []})
            old_version = record.get("version")
            attempts = record["attempts"]
            # Query all historical attempts, including terminal ones: an answer
            # may have committed before its former generation's final turn.
            live, answers = [], []
            for attempt in attempts:
                status = await self.rpc.prompt_status_once(self.config, attempt["question_id"])
                if status.get("type") == "prompt.error" and status.get("error_code") == "question_not_found" and attempt.get("state") in {"intent", "blocked"}:
                    attempt["observed_state"] = "absent"
                    continue
                checked(status, "prompt.status.ok")
                question = status["question"]
                attempt["observed_state"] = question["state"]
                if question["state"] == "answered":
                    attempt["answer"] = question["answer"]
                    if attempt["version"] == version:
                        answers.append(attempt)
                    else:
                        attempt["stale_answer_refused"] = True
                elif question["state"] == "open":
                    live.append(attempt)
                elif question["state"] not in {"expired", "cancelled", "canceled"}:
                    raise RuntimeError("unknown question state; no new ask")
            if len(live) > 1:
                raise RuntimeError("multiple live questions; escalate trace before any new ask")
            if live and live[0]["version"] != version:
                reply = await self.rpc.prompt_cancel_once(self.config, {"type": "prompt.cancel", "question_id": live[0]["question_id"], "reason": "proposal_scope_changed"})
                checked(reply, "prompt.cancel.ok")
                status = checked(await self.rpc.prompt_status_once(self.config, live[0]["question_id"]), "prompt.status.ok")
                if status["question"]["state"] == "open":
                    raise RuntimeError("old-version live question cannot be retired")
                live = []
            record.update({k: proposal[k] for k in proposal if k not in {"attempts", "answer", "version", "state"}})
            record["version"] = version
            if answers:
                if len({digest(a["answer"]) for a in answers}) != 1:
                    raise RuntimeError("conflicting current-version answers")
                record.update(state="answered", answer=answers[-1]["answer"], answer_question_id=answers[-1]["question_id"])
            elif live:
                record.update(state="pending")
                record.pop("answer", None)
            elif disposition == "propose" and old_version == version and record.get("state") == "ask_blocked" and not retry_blocked and any(
                    a.get("state") == "blocked" and a["version"] == version for a in attempts):
                attempt = next(a for a in reversed(attempts) if a.get("state") == "blocked" and a["version"] == version)
                return await self._blocked_report(path, preimage, records, record, attempt)
            elif old_version == version and record.get("state") in {"rejected", "deferred", "authorized", "shipped"}:
                pass
            elif proposal.get("disposition") in {"resolved", "duplicate", "no_change", "authorized", "investigate", "defer"}:
                record["state"] = proposal["disposition"]
                record.pop("answer", None)
            else:
                question_id = "retro-q-" + digest([identity, version, binding["session_generation"]])[:48]
                attempt = next((a for a in attempts if a["question_id"] == question_id), None)
                if attempt is None:
                    attempt = {"question_id": question_id, "version": version, "producer": binding["stream_id"],
                               "generation": binding["session_generation"], "state": "intent"}
                    attempts.append(attempt)
                envelope = prompt_protocol.build_envelope(
                    title=proposal["title"], body=proposal["body"], response_mode="single_choice",
                    raw_options=[prompt_protocol.PromptOption(**option) for option in proposal["options"]],
                    question_id=question_id, dedup_key=question_id, producer_stream_id=binding["stream_id"], spec_id=work_id)
                record.update(state="pending")
                record.pop("answer", None)
                records[identity] = record
                preimage = save_proposals(path, preimage, records)  # Durable intent BEFORE ask.
                reply = await self.rpc.prompt_ask_once(self.config, {"type": "prompt.ask", "envelope": envelope,
                                                       "actions": prompt_protocol.notification_actions(envelope),
                                                       "request_id": question_id})
                if reply.get("type") == "prompt.error" and reply.get("error_code") == "ask_parent":
                    attempt.update(state="blocked", refusal=reply, blocked_at=now_iso())
                    record.update(state="ask_blocked")
                    return await self._blocked_report(path, preimage, records, record, attempt)
                checked(reply, "prompt.ask.ok")
                attempt.update(state="asked", receipt=reply, asked_at=now_iso())
            records[identity] = record
            save_proposals(path, preimage, records)
            return record


def work_path(settings, work_id):
    if not isinstance(work_id, str) or not re.fullmatch(r"[A-Za-z0-9_:-]+", work_id):
        raise ValueError("normal work ID required")
    found = []
    for path in (settings.memory_root / "work").glob("*/*/spec.md"):
        try:
            if parse_frontmatter(path).get("id") == work_id:
                resolved = path.resolve()
                if settings.memory_root not in resolved.parents:
                    raise ValueError("work file escapes memory root")
                found.append(path)
        except (OSError, UnicodeError, RuntimeError):
            continue
    if len(found) != 1:
        raise ValueError("normal work ID must resolve exactly once; create with existing triage first")
    return found[0]


def proposals(text):
    if PROPOSAL_START not in text:
        return {}
    block = text.split(PROPOSAL_START, 1)[1].split(PROPOSAL_END, 1)[0].strip()
    return json.loads(block.removeprefix("```json\n").removesuffix("\n```"))


def save_proposals(path, preimage, records):
    if path.read_bytes() != preimage:
        raise RuntimeError("work preimage moved; no mutation")
    text = preimage.decode()
    block = PROPOSAL_START + "\n```json\n" + encoded(records).decode() + "\n```\n" + PROPOSAL_END
    if PROPOSAL_START in text:
        start = text.index(PROPOSAL_START)
        end = text.index(PROPOSAL_END, start) + len(PROPOSAL_END)
        text = text[:start] + block + text[end:]
    else:
        text += "\n\n" + block + "\n"
    atomic(path, text.encode())
    return text.encode()


class ProducerTransport:
    """Existing operator RPCs restricted to owned workers and frozen REPORTs."""
    ALLOWED = frozenset({"spawn", "await_spawn", "await_report", "close",
                         "assistant.binding", "send.receipt.get", "send"})
    def __init__(self, settings):
        self.settings = settings

    def stages(self):
        for name in STAGES:
            for path in (self.settings.state_root / "runs").glob(f"*/{name}.json"):
                yield read(path)

    def owned(self, stream, generation=None):
        return any(stage.get("stream_id") == stream and stage.get("generation")
                   and (generation is None or stage["generation"] == generation) for stage in self.stages())

    async def call(self, payload, timeout=30):
        verb = payload["type"]
        if verb not in self.ALLOWED:
            raise ValueError("unattended operator verb outside explicit allowlist")
        body = {k: v for k, v in payload.items() if k != "type"}
        if verb == "spawn" and not any(stage.get("payload") == body for stage in self.stages()):
            raise ValueError("spawn outside retained intent")
        if verb == "await_spawn" and not any(stage.get("payload", {}).get("request_id") == body.get("spawn_request_id") for stage in self.stages()):
            raise ValueError("await outside retained admission")
        if verb == "await_report" and (body.get("msg_id") != 0 or not self.owned(body.get("stream_id"))):
            raise ValueError("await outside owned worker")
        if verb == "close" and (not body.get("expected_generation") or not self.owned(
                f"{body.get('host')}:{body.get('session_name')}", body["expected_generation"])):
            raise ValueError("close outside generation ownership")
        if verb == "assistant.binding" and body:
            raise ValueError("binding read has no mutable fields")
        if verb == "send" and (not body.get("text", "").startswith("REPORT daily-retro ") or not any(a["payload"] == body for a in self.deliveries())):
            raise ValueError("send outside retained REPORT")
        if verb == "send.receipt.get" and not any(a["target"] == body.get("to_stream_id") and a["request_id"] == body.get("request_id") for a in self.deliveries()):
            raise ValueError("receipt outside retained REPORT")
        def invoke():
            with authenticated_operator_connection(self.settings.ws_url, self.settings.token_path, timeout + 2) as connection:
                return connection.rpc(payload)
        return await asyncio.to_thread(invoke)

    async def spawn_once(self, config, payload):
        if not any(stage.get("payload") == payload for stage in self.stages()):
            raise ValueError("spawn must match retained run intent")
        if payload.get("provider") != "codex" or not payload.get("self_close_on_completion") or payload.get("parent_stream_id"):
            raise ValueError("only transient self-closing top-level Codex workers")
        response = await self.call({**payload, "type": "spawn"}, 180)
        if response.get("type") == "spawn.ok" and response.get("state") == "starting":
            response = await self.call({"type": "await_spawn", "spawn_request_id": payload["request_id"],
                                        "request_id": payload["request_id"] + "-await"}, 180)
            if response.get("type") == "await_spawn.ok":
                response = {**response, "type": "spawn.ok"}
        return response

    async def await_report_once(self, config, stream, msg_id, **kwargs):
        if not self.owned(stream) or msg_id != 0:
            raise ValueError("await must target an owned generation's terminal report")
        return await self.call({"type": "await_report", "stream_id": stream, "msg_id": 0,
                                "include_details": True, "include_extras": True,
                                "timeout": kwargs.get("timeout", 30)}, kwargs.get("timeout", 30))

    async def close_once(self, config, stream, **kwargs):
        generation = kwargs.get("expected_generation")
        if not generation or not self.owned(stream, generation):
            raise ValueError("cleanup requires exact retained ownership")
        host, name = stream.split(":", 1)
        return await self.call({"type": "close", "host": host, "session_name": name,
                                "expected_generation": generation, "reason": "report_terminate"})

    def deliveries(self):
        for name in ("delivery.json", "failure-delivery.json"):
            for path in (self.settings.state_root / "runs").glob(f"*/{name}"):
                yield from read(path)["attempts"]

    async def assistant_once(self, config, payload):
        if payload != {"type": "assistant.binding"}:
            raise ValueError("producer may only read current binding")
        return await self.call(payload)

    async def send_once(self, config, payload):
        if not payload.get("text", "").startswith("REPORT daily-retro ") or not any(a["payload"] == payload for a in self.deliveries()):
            raise ValueError("send must exactly match retained REPORT target/body/key")
        return await self.call({**payload, "type": "send"})

    async def send_receipt_once(self, config, target, key):
        if not any(a["target"] == target and a["request_id"] == key for a in self.deliveries()):
            raise ValueError("receipt must match retained REPORT attempt")
        return await self.call({"type": "send.receipt.get", "to_stream_id": target, "request_id": key})


async def rehearse(settings, workers, evidence_dir):
    """Real worker reports, restricted to a running isolated assistant counterpart."""
    if not settings.isolated or settings.memory_root == workers.memory_root:
        raise ValueError("rehearsal requires separate fixture memory and isolated current-assistant endpoint")
    worker_settings = replace(workers, memory_root=settings.memory_root, state_root=settings.state_root)
    owned = ProducerTransport(worker_settings)
    delivery = ProducerTransport(settings)

    class RehearsalTransport:
        spawn_once = owned.spawn_once
        await_report_once = owned.await_report_once
        close_once = owned.close_once
        assistant_once = delivery.assistant_once
        send_once = delivery.send_once
        send_receipt_once = delivery.send_receipt_once

    pipeline = Pipeline(replace(settings, host=workers.host), RehearsalTransport())
    with locked(settings.state_root / "run.lock"):
        manifest = collect(settings, datetime.now(timezone.utc))
        required = {"spec_fixture_repeat_a", "spec_fixture_repeat_b", "spec_fixture_serious",
                    "spec_fixture_fixed", "spec_fixture_owned", "spec_fixture_uncertain"}
        if {s["id"] for s in manifest["sources"]} != required:
            raise ValueError("rehearsal must use the six pinned analytical fixtures")
        try:
            sol = await pipeline.worker(manifest, "sol")
            challenge_path = settings.state_root / "runs" / manifest["run_id"] / "challenge.json"
            challenge = read(challenge_path)
            if not challenge:
                draft = json.loads(json.dumps(sol["packet"]))
                # A declared fixture mutation at the analyst/finalizer seam:
                # preserve the original report and every source disposition.
                draft["candidates"] = [c for c in draft["candidates"] if "spec_fixture_serious" not in c["citations"]]
                for candidate in draft["candidates"]:
                    if "spec_fixture_repeat_a" in candidate["citations"]:
                        candidate["consequence"] = "[fixture gap: check original measurable consequence]"
                challenge = {"sol_original_hash": sol["packet_hash"], "draft": draft,
                             "scope": "planted shortlist omission and small evidence gap; all dispositions/originals retained"}
                atomic(challenge_path, challenge)
            astra = await pipeline.worker(manifest, "astra", challenge["draft"])
            candidates = astra["packet"]["candidates"]
            serious = [c for c in candidates if "spec_fixture_serious" in c["citations"]]
            repeated = [c for c in candidates if {"spec_fixture_repeat_a", "spec_fixture_repeat_b"} <= set(c["citations"])]
            uncertain = [c for c in candidates if "spec_fixture_uncertain" in c["citations"] and c["uncertainty"]]
            if not serious or not repeated or not uncertain or any("[fixture gap:" in c["consequence"] for c in repeated):
                raise RuntimeError("Astra analytical fixture acceptance failed; retain exact reports for QA")
            await pipeline.deliver(manifest, astra)
            receipt = {"run_id": manifest["run_id"], "sol_report_id": sol["report"]["report_id"],
                       "astra_report_id": astra["report"]["report_id"], "challenge_hash": digest(challenge),
                       "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "isolated_url": settings.ws_url, "workers_url": workers.ws_url,
                       "review": read(settings.state_root / "runs" / manifest["run_id"] / "review.json"),
                       "scope": "real Sol/Astra; isolated Codex assistant/provider counterpart, synthetic questions excluded"}
            atomic(Path(evidence_dir) / "rehearsal.json", receipt)
            return receipt
        finally:
            await pipeline.cleanup(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "run", "record-review", "decision", "rehearse", "history-collect", "history-run", "history-consolidate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", required=True)
        if name.startswith("history-"):
            cmd.add_argument("--baseline", required=True)
            if name == "history-consolidate":
                group = cmd.add_mutually_exclusive_group(required=True)
                group.add_argument("--batches", help="comma-separated completed batch numbers, one to five")
                group.add_argument("--final", action="store_true", help="one final Astra review of the complete inventory")
            else:
                cmd.add_argument("--batch", required=True, type=int)
                if name == "history-run":
                    cmd.add_argument("--no-deliver", action="store_true", help="retain serial-context packets without any delivery")
        elif name == "collect":
            cmd.add_argument("--now", required=True)
        elif name == "run":
            cmd.add_argument("--on-demand", action="store_true")
        elif name == "record-review":
            cmd.add_argument("--run-id", required=True)
            cmd.add_argument("--result", required=True, help="JSON file path")
        elif name == "decision":
            cmd.add_argument("--work-id", required=True)
            cmd.add_argument("--proposal", required=True, help="JSON file path")
            cmd.add_argument("--retry-blocked", action="store_true", help="Resume one blocked ask after daemon admission is live")
        else:
            cmd.add_argument("--workers-config", required=True)
            cmd.add_argument("--evidence-dir", required=True)
    args = parser.parse_args()
    settings = Settings.load(args.config)
    if args.command == "collect":
        result = collect(settings, datetime.fromisoformat(args.now))
    elif args.command == "history-collect":
        result = history_collect(settings, args.baseline, args.batch)
    elif args.command == "rehearse":
        result = asyncio.run(rehearse(settings, Settings.load(args.workers_config), args.evidence_dir))
    else:
        pipeline = Pipeline(settings, ProducerTransport(settings)) if args.command in {"run", "history-run", "history-consolidate"} else Pipeline(settings)
        if args.command == "run":
            result = asyncio.run(pipeline.run(on_demand=args.on_demand))
        elif args.command == "history-run":
            result = asyncio.run(pipeline.history_run(args.baseline, args.batch, no_deliver=args.no_deliver))
        elif args.command == "history-consolidate":
            batches = [int(n) for n in args.batches.split(",")] if args.batches else None
            result = asyncio.run(pipeline.history_consolidate(args.baseline, batches, final=args.final))
        elif args.command == "record-review":
            result = asyncio.run(pipeline.record_review(args.run_id, read(Path(args.result))))
        else:
            result = asyncio.run(pipeline.decision(args.work_id, read(Path(args.proposal)), retry_blocked=args.retry_blocked))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
