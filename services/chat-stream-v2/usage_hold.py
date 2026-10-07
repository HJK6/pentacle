"""Satellite held-span file: usage records that no fence covered yet.

A record read before its stream has a coordinator usage fence (or after the
fence row closed) is kept here, durably and bounded, until a wire-v2 daemon
resolves its generation from history (docs/usage_accounting.md § Unfenced
spans). Every drop is first recorded as a loss record in the same atomic
write; loss records follow the lifecycle of spec AC10 Target State 2a:

* identity ``instance:seq``, never reused (a new file mints a new instance);
* same ``(key, reason)`` merges into the present record (counts add, rev+1);
* at most 64 records: 60 keyed plus one reserved overflow record per reason;
* removed only when an ack's ``rev`` >= the local ``rev``; an id the daemon
  reports as ``losses_conflict`` is retained as is (never re-minted), and the
  first conflict rolls the instance for ids minted afterwards.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("chat_streamd_v2.usage_hold")

DEFAULT_PATH = "~/.local/state/pentacle-satellite/unfenced_usage.json"
FILE_VERSION = 1
MAX_ENTRIES = 64
MAX_ENTRY_RECORDS = 256
MAX_ENTRY_BYTES = 64 * 1024
ENABLED_TTL_S = 24 * 3600.0
DISABLED_RETAIN_S = 7 * 86400.0
LOSS_REASONS = (
    "held_span_expired_ttl", "held_span_capacity_entries",
    "held_span_capacity_bytes", "held_span_overflow_records",
)
MAX_LOSSES = 64
KEYED_LOSS_SLOTS = MAX_LOSSES - len(LOSS_REASONS)
IDENTITY_FIELDS = ("stream_id", "provider", "source_pane_pid", "native_session_id", "source_file_identity_digest")
#: Persist accumulated enabled time at most this often when nothing else changed.
ENABLED_FLUSH_S = 60.0


def entry_key(identity: dict[str, Any]) -> str:
    raw = "\0".join(str(identity[field]) for field in ("stream_id", "source_pane_pid", "native_session_id",
                                                       "source_file_identity_digest"))
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def _record_bytes(record: dict[str, Any]) -> int:
    return len(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())


def _fresh_state() -> dict[str, Any]:
    return {"version": FILE_VERSION, "instance": secrets.token_hex(16), "next_loss_seq": 1,
            "next_record_seq": 1, "conflict_seen": False, "entries": {}, "losses": []}


class HeldSpans:
    """The durable hold file; every mutation is one temp+fsync+rename write."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_PATH, *, wall=time.time) -> None:
        self.path = Path(os.path.expanduser(str(path)))
        self._wall = wall
        self._unflushed_s = 0.0
        self.state = self._load()
        if not self.path.exists():
            # The instance is minted when the file is first created, so it is
            # durable before any id is issued from it.
            self._write(self.state)

    # -- persistence --------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            return _fresh_state()
        except (OSError, ValueError) as exc:
            # An unreadable file cannot be trusted for ids; mint a new
            # instance (ids are never reused) and keep the old bytes aside.
            log.warning("held-span file unreadable (%s); starting a new instance", exc)
            try:
                self.path.rename(self.path.with_suffix(f".corrupt-{int(time.time())}"))
            except OSError:
                pass
            return _fresh_state()
        if not isinstance(state, dict) or state.get("version") != FILE_VERSION:
            log.warning("held-span file has an unknown shape; starting a new instance")
            return _fresh_state()
        state.setdefault("entries", {})
        state.setdefault("losses", [])
        state.setdefault("conflict_seen", False)
        state.setdefault("next_record_seq", 1)
        return state

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.path)
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        self.state = state
        self._unflushed_s = 0.0

    # -- losses ---------------------------------------------------------------

    @staticmethod
    def _record_loss(state: dict[str, Any], *, key: str, provider: str, native: str, reason: str,
                     records: int, nbytes: int, at: float) -> None:
        losses = state["losses"]
        hit = next((loss for loss in losses if loss["key"] == key and loss["reason"] == reason), None)
        if hit is None and sum(1 for loss in losses if loss["key"] != "*") >= KEYED_LOSS_SLOTS:
            key, provider, native = "*", "*", "*"
            hit = next((loss for loss in losses if loss["key"] == "*" and loss["reason"] == reason), None)
        if hit is None:
            seq = state["next_loss_seq"]
            state["next_loss_seq"] = seq + 1
            hit = {"loss_id": f"{state['instance']}:{seq}", "key": key, "provider": provider,
                   "native_session_id": native, "reason": reason, "coalesced": key == "*",
                   "records_lost": 0, "bytes_lost": 0, "occurrences": 0,
                   "first_at": at, "last_at": at, "rev": 0}
            losses.append(hit)
        hit["records_lost"] += records
        hit["bytes_lost"] += nbytes
        hit["occurrences"] += 1
        hit["last_at"] = at
        hit["rev"] += 1
        assert len(losses) <= MAX_LOSSES, "loss buffer bound exceeded"

    def _drop_entry(self, state: dict[str, Any], key: str, reason: str, at: float) -> None:
        entry = state["entries"].pop(key)
        self._record_loss(state, key=key, provider=entry["provider"], native=entry["native_session_id"],
                          reason=reason, records=len(entry["records"]),
                          nbytes=sum(_record_bytes(item["record"]) for item in entry["records"]), at=at)

    # -- writes -----------------------------------------------------------------

    def hold(self, identity: dict[str, Any], records: list[dict[str, Any]], clock: dict | None) -> int:
        """Append sanitized records (each carrying ``transcript_ts``) to the
        identity's entry, fsynced before the caller's events push. Exact
        duplicates (a re-read span) are not appended twice. Returns the count
        newly held."""
        if not records:
            return 0
        state = copy.deepcopy(self.state)
        now = self._wall()
        key = entry_key(identity)
        entry = state["entries"].get(key)
        if entry is None:
            entry = {**{field: str(identity[field]) for field in IDENTITY_FIELDS},
                     "written_at": now, "enabled_s": 0.0, "records": []}
            state["entries"][key] = entry
        seen = {item["digest"] for item in entry["records"]}
        size = sum(_record_bytes(item["record"]) for item in entry["records"])
        added = 0
        for record in records:
            digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
            if digest in seen:
                continue
            nbytes = _record_bytes(record)
            if len(entry["records"]) >= MAX_ENTRY_RECORDS:
                self._record_loss(state, key=key, provider=entry["provider"], native=entry["native_session_id"],
                                  reason="held_span_overflow_records", records=1, nbytes=nbytes, at=now)
                continue
            if size + nbytes > MAX_ENTRY_BYTES:
                self._record_loss(state, key=key, provider=entry["provider"], native=entry["native_session_id"],
                                  reason="held_span_capacity_bytes", records=1, nbytes=nbytes, at=now)
                continue
            seq = state["next_record_seq"]
            state["next_record_seq"] = seq + 1
            entry["records"].append({"seq": seq, "digest": digest, "record": record, "clock": clock})
            seen.add(digest)
            size += nbytes
            added += 1
        if not entry["records"]:
            state["entries"].pop(key, None)
        while len(state["entries"]) > MAX_ENTRIES:
            oldest = min((item for item in state["entries"].items() if item[0] != key),
                         key=lambda item: item[1]["written_at"])[0]
            self._drop_entry(state, oldest, "held_span_capacity_entries", now)
        if state != self.state:
            self._write(state)
        return added

    def tick(self, *, enabled: bool, elapsed_s: float) -> None:
        """Advance enabled time and drop expired entries (24 h enabled, or
        7 days from write while the daemon cannot take them).

        Enabled time accrues in memory and is persisted with the next write
        (at least every ENABLED_FLUSH_S); a restart can only lose accrual,
        which keeps an entry longer, never shorter.
        """
        entries = self.state["entries"]
        if not entries:
            return
        now = self._wall()
        if enabled and elapsed_s > 0:
            for entry in entries.values():
                entry["enabled_s"] = float(entry.get("enabled_s", 0.0)) + elapsed_s
            self._unflushed_s += elapsed_s
        expired = [key for key, entry in entries.items()
                   if entry["enabled_s"] > ENABLED_TTL_S or now - entry["written_at"] > DISABLED_RETAIN_S]
        if not expired and self._unflushed_s < ENABLED_FLUSH_S:
            return
        state = copy.deepcopy(self.state)
        for key in expired:
            self._drop_entry(state, key, "held_span_expired_ttl", now)
        self._write(state)

    def bind_clock(self, clock: dict) -> None:
        """Bind the first sample obtained after capture to clockless records."""
        if not any(item["clock"] is None for entry in self.state["entries"].values() for item in entry["records"]):
            return
        state = copy.deepcopy(self.state)
        for entry in state["entries"].values():
            for item in entry["records"]:
                if item["clock"] is None:
                    item["clock"] = clock
        self._write(state)

    # -- wire ---------------------------------------------------------------------

    def frame_entries(self, budget_bytes: int) -> list[dict[str, Any]]:
        """Entries for one frame within a byte budget; clockless records wait."""
        out: list[dict[str, Any]] = []
        for key, entry in sorted(self.state["entries"].items(), key=lambda item: item[1]["written_at"]):
            records = [{**item["record"], "seq": item["seq"], "clock": item["clock"]}
                       for item in entry["records"] if item["clock"] is not None]
            if not records:
                continue
            wire = {"key": key, **{field: entry[field] for field in IDENTITY_FIELDS}, "records": records}
            size = len(json.dumps(wire)) + 2
            if size > budget_bytes:
                break
            budget_bytes -= size
            out.append(wire)
        return out

    def frame_losses(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.state["losses"])

    def apply_ack(self, block: dict[str, Any], losses_recorded: Any, losses_conflict: Any) -> None:
        """Delete recorded/terminal records and acked losses; keep the rest."""
        if not (block.get("recorded") or block.get("rejected") or losses_recorded or losses_conflict):
            return
        state = copy.deepcopy(self.state)
        done: dict[str, set[int]] = {}
        for item in block.get("recorded") or ():
            if isinstance(item, dict) and type(item.get("seq")) is int:
                done.setdefault(str(item.get("key")), set()).add(item["seq"])
        whole: set[str] = set()
        for item in block.get("rejected") or ():
            if not isinstance(item, dict) or item.get("transient"):
                continue
            if item.get("seq") is None:
                whole.add(str(item.get("key")))
            elif type(item.get("seq")) is int:
                done.setdefault(str(item.get("key")), set()).add(item["seq"])
        for key in list(state["entries"]):
            entry = state["entries"][key]
            if key in whole:
                state["entries"].pop(key)
                continue
            seqs = done.get(key)
            if seqs:
                entry["records"] = [item for item in entry["records"] if item["seq"] not in seqs]
                if not entry["records"]:
                    state["entries"].pop(key)
        acked = {item["loss_id"]: item["rev"] for item in (losses_recorded or ())
                 if isinstance(item, dict) and isinstance(item.get("loss_id"), str) and type(item.get("rev")) is int}
        conflicts = [item for item in (losses_conflict or ()) if isinstance(item, dict)]
        conflicted = {item.get("loss_id") for item in conflicts}
        state["losses"] = [loss for loss in state["losses"]
                           if loss["loss_id"] in conflicted
                           or not (loss["loss_id"] in acked and loss["rev"] <= acked[loss["loss_id"]])]
        if conflicts and not state.get("conflict_seen"):
            # Ids were issued before (an older hold file). Retain the
            # conflicted records as they are; ids minted from now on use a
            # fresh instance so later genuine losses never collide.
            state["instance"] = secrets.token_hex(16)
            state["next_loss_seq"] = 1
            state["conflict_seen"] = True
            log.warning("held-span loss conflict: %s; instance rolled",
                        sorted(str(item.get("loss_id")) for item in conflicts))
        if state != self.state:
            self._write(state)

    def move_fenced(self, item: dict[str, Any], indices: list[int], clock: dict | None) -> int:
        """Hold the records of a fenced item the daemon deferred (clock)."""
        records = [item["records"][index] for index in indices
                   if 0 <= index < len(item["records"]) and item["records"][index].get("type") != "session_meta"]
        identity = {field: item[field] for field in IDENTITY_FIELDS}
        return self.hold(identity, records, clock)

    def pending_count(self) -> int:
        return sum(len(entry["records"]) for entry in self.state["entries"].values())
