"""Daemon progress and bounded typed handoff for an independent checker.

No transport lives here. Only the external checker owns episode allocation and
submission. A stale publisher must be detected by that separate process.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

log = logging.getLogger("chat_streamd_v2.loop_watchdog")
INTERVAL_S = 1.0
STALL_S = 5.0
MAX_BYTES = 4096
CAUSES = frozenset({"loop", "store", "loop_store", "unavailable"})
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


@dataclass
class RequestTiming:
    """A request-owned aggregate, never a global last-request measurement."""
    calls: int = 0
    queue_s: float = 0.0
    execution_s: float = 0.0
    closed: bool = False

    def add(self, queued: float, started: float, finished: float) -> None:
        if not self.closed:
            self.calls += 1
            self.queue_s += max(0.0, started - queued)
            self.execution_s += max(0.0, finished - started)

    def fields(self) -> dict:
        return {"store_calls": self.calls, "store_queue_ms": round(self.queue_s * 1000, 3),
                "store_execution_ms": round(self.execution_s * 1000, 3)}


request_timing: ContextVar[RequestTiming | None] = ContextVar("store_request_timing", default=None)


class StoreProgress:
    """Queue-capped O(1) updates; no callback body, SQL, logging or file I/O."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending = deque()
        self.enqueued_seq = self.started_seq = self.finished_seq = 0
        self.current_started = None

    def enqueue(self, enqueue, item) -> None:
        # Publish queue admission and its metrics together. The worker cannot
        # start metric bookkeeping until this bounded critical section ends.
        with self.lock:
            enqueue(item)  # put_nowait; a refused item changes no counters
            self.pending.append(item[3])
            self.enqueued_seq += 1

    def start(self, now: float) -> None:
        with self.lock:
            self.pending.popleft()
            self.started_seq += 1
            self.current_started = now

    def finish(self) -> None:
        with self.lock:
            self.finished_seq += 1
            self.current_started = None

    def discard(self) -> None:
        # A shutdown-refused queued callback is finished without execution.
        with self.lock:
            self.pending.popleft()
            self.started_seq += 1
            self.finished_seq += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {"enqueued_seq": self.enqueued_seq, "started_seq": self.started_seq,
                    "finished_seq": self.finished_seq, "pending_count": len(self.pending),
                    "oldest_pending_mono_s": self.pending[0] if self.pending else None,
                    "current_started_mono_s": self.current_started}


def _parent_fd(path: Path) -> int:
    """Walk every component without following symlinks; pin the actual parent."""
    if not path.is_absolute() or '..' in path.parts or path.name in ('', '.', '..'):
        raise ValueError("invalid_monitor_path")
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("insecure_monitor_directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_file(info) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("insecure_monitor_file")


def read_json(path: Path) -> dict:
    parent = _parent_fd(path)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            _check_file(info)
            if info.st_size > MAX_BYTES:
                raise ValueError("oversized_monitor_file")
            data = os.read(fd, MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("oversized_monitor_file")
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalid_monitor_document")
    return value


def write_json(path: Path, value: dict) -> None:
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("oversized_monitor_file")
    parent = _parent_fd(path)
    temporary = '.' + path.name + '.' + uuid.uuid4().hex + '.tmp'
    try:
        try:
            _check_file(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def _diagnostic(message, *args) -> None:
    # A broken diagnostic handler must not stop progress or handoff retry.
    try:
        log.warning(message, *args)
    except Exception:
        pass


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_snapshot(value: dict, now: float) -> dict:
    """Strict public wire validation; invalid state is never healthy progress."""
    if set(value) != {'version', 'instance_id', 'boot_id', 'pid', 'sample_mono_s',
                      'loop_seq', 'loop_mono_s', 'store'} or value['version'] != 'daemon-progress.v1':
        raise ValueError('invalid_progress_version')
    if not isinstance(value['instance_id'], str) or not _ID.fullmatch(value['instance_id']):
        raise ValueError('invalid_progress_identity')
    if not isinstance(value['boot_id'], str) or str(uuid.UUID(value['boot_id'])) != value['boot_id']:
        raise ValueError('invalid_progress_boot')
    for field in ('pid', 'loop_seq'):
        if type(value[field]) is not int or value[field] < (1 if field == 'pid' else 0):
            raise ValueError('invalid_progress_counter')
    sample = value['sample_mono_s']
    if not _number(sample) or sample > now or not _number(value['loop_mono_s']) or value['loop_mono_s'] > sample:
        raise ValueError('invalid_progress_time')
    store = value['store']
    if not isinstance(store, dict) or set(store) != {'enqueued_seq', 'started_seq', 'finished_seq',
            'pending_count', 'oldest_pending_mono_s', 'current_started_mono_s'}:
        raise ValueError('invalid_store_progress')
    for name in ('enqueued_seq', 'started_seq', 'finished_seq', 'pending_count'):
        if type(store[name]) is not int or store[name] < 0:
            raise ValueError('invalid_store_counter')
    enqueued, started, finished = (store[n] for n in ('enqueued_seq', 'started_seq', 'finished_seq'))
    if not (enqueued >= started >= finished and started - finished <= 1 and
            store['pending_count'] == enqueued - started):
        raise ValueError('impossible_store_counters')
    for name, active in (('oldest_pending_mono_s', store['pending_count'] > 0),
                         ('current_started_mono_s', started > finished)):
        val = store[name]
        if (active and (not _number(val) or val > sample)) or (not active and val is not None):
            raise ValueError('invalid_store_time')
    return value


def cause_at(value: dict, now: float) -> str | None:
    validate_snapshot(value, now)
    if now - value['sample_mono_s'] >= STALL_S:
        return 'unavailable'
    loop = now - value['loop_mono_s'] >= STALL_S
    store = any(at is not None and now - at >= STALL_S for at in
                (value['store']['oldest_pending_mono_s'], value['store']['current_started_mono_s']))
    return 'loop_store' if loop and store else 'loop' if loop else 'store' if store else None


def episode_id(instance: str, boot: str, ordinal: int) -> str:
    return hashlib.sha256(f'{instance}\0{boot}\0{ordinal}'.encode()).hexdigest()


def validate_handoff(value: dict, instance: str) -> dict:
    if (set(value) != {'version', 'instance_id', 'active', 'recovery'}
            or value['version'] != 'daemon-episodes.v1' or value['instance_id'] != instance):
        raise ValueError('invalid_episode_handoff')
    for key, condition in (('active', 'active'), ('recovery', 'recovered')):
        row = value[key]
        if row is None:
            continue
        if not isinstance(row, dict) or set(row) != {'boot_id', 'ordinal', 'episode_id', 'condition', 'cause'}:
            raise ValueError('invalid_episode_record')
        if (not isinstance(row['boot_id'], str) or str(uuid.UUID(row['boot_id'])) != row['boot_id'] or type(row['ordinal']) is not int
                or not 0 < row['ordinal'] < 2**63 or row['condition'] != condition
                or row['cause'] not in CAUSES
                or row['episode_id'] != episode_id(instance, row['boot_id'], row['ordinal'])):
            raise ValueError('invalid_episode_identity')
    # A recovery must carry its opening fact until acknowledged; this allows a
    # stalled daemon to replay active -> recovered without losing suppression.
    if value['recovery'] and (not value['active'] or
            value['active']['episode_id'] != value['recovery']['episode_id']):
        raise ValueError('recovery_without_active')
    return value


class LoopWatchdog:
    """One bounded publisher thread, loop pulse and serialized handoff consumer."""
    def __init__(self, store, alerts, *, progress_path: Path | None = None,
                 instance_id: str | None = None):
        self.store, self.alerts = store, alerts
        if bool(progress_path) != bool(instance_id) or (instance_id and not _ID.fullmatch(instance_id)):
            raise ValueError('invalid_monitor_configuration')
        self.path, self.instance_id = progress_path, instance_id or 'unbound'
        self.boot_id = str(uuid.uuid4())
        self.lock = threading.Lock()
        self.loop_seq, self.loop_at = 0, time.monotonic()
        self.handoff = None
        self.ack = {'version': 'daemon-episode-ack.v1', 'instance_id': self.instance_id,
                    'active': None, 'terminal': None}
        self.ack_loaded = False
        self.ack_dirty = False
        self.stop_event = threading.Event()
        self.thread = None
        self.pulse_handle = None
        self.last_diagnostic = None
        self.binding_available = False

    @classmethod
    def from_env(cls, store, alerts):
        path = os.environ.get('PENTACLE_DAEMON_PROGRESS_PATH')
        return cls(store, alerts, progress_path=Path(path) if path else None,
                   instance_id=os.environ.get('PENTACLE_DAEMON_INSTANCE_ID'))

    def start(self, loop) -> None:
        if self.thread is not None:
            raise RuntimeError('monitor_already_started')
        def pulse():
            with self.lock:
                self.loop_seq += 1
                self.loop_at = time.monotonic()
            if not self.stop_event.is_set():
                self.pulse_handle = loop.call_later(INTERVAL_S, pulse)
        pulse()
        self.thread = threading.Thread(target=self._publish, name='loop-progress', daemon=True)
        self.thread.start()

    def snapshot(self) -> dict:
        with self.lock:
            seq, at = self.loop_seq, self.loop_at
        store = self.store.progress.snapshot()
        return {'version': 'daemon-progress.v1', 'instance_id': self.instance_id,
                'boot_id': self.boot_id, 'pid': os.getpid(), 'sample_mono_s': time.monotonic(),
                'loop_seq': seq, 'loop_mono_s': at, 'store': store}

    def _exchange(self, snapshot) -> None:
        if self.path is None:
            return
        write_json(self.path, snapshot)
        ack_path = self.path.with_name(self.path.name + '.ack.json')
        if not self.ack_loaded:
            try:
                ack = read_json(ack_path)
            except FileNotFoundError:
                ack = self.ack
            if (set(ack) != {'version', 'instance_id', 'active', 'terminal'}
                    or ack['version'] != 'daemon-episode-ack.v1' or ack['instance_id'] != self.instance_id
                    or any(v is not None and (not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v))
                           for v in (ack['active'], ack['terminal']))):
                raise ValueError('invalid_episode_ack')
            with self.lock:
                self.ack = ack
                self.ack_loaded = True
        with self.lock:
            dirty, ack = self.ack_dirty, dict(self.ack)
        if dirty:
            write_json(ack_path, ack)
            with self.lock:
                if self.ack == ack:
                    self.ack_dirty = False
        handoff = validate_handoff(read_json(self.path.with_name(self.path.name + '.episodes.json')), self.instance_id)
        with self.lock:
            # The sender must not advance to a new episode before terminal ACK.
            active = handoff['active']
            prior = self.handoff and self.handoff['active']
            if (prior and self.ack['terminal'] != prior['episode_id'] and
                    (not active or active['episode_id'] != prior['episode_id'])):
                raise ValueError('unacknowledged_episode_replaced')
            if active and self.ack['active'] not in (None, active['episode_id'], self.ack['terminal']):
                raise ValueError('unacknowledged_episode_replaced')
            self.handoff = handoff

    def _publish(self) -> None:
        while not self.stop_event.is_set():
            snapshot = self.snapshot()
            cause = cause_at(snapshot, snapshot['sample_mono_s'])
            try:
                self._exchange(snapshot)
                self.binding_available = self.path is not None
            except (OSError, ValueError, TypeError, KeyError):
                self.binding_available = False
            diagnostic = (cause, self.binding_available)
            if diagnostic != self.last_diagnostic:
                _diagnostic('daemon_progress cause=%s checker_handoff_available=%s protected=false',
                            cause or 'healthy', self.binding_available)
                self.last_diagnostic = diagnostic
            self.stop_event.wait(INTERVAL_S)

    async def consume_once(self) -> None:
        with self.lock:
            handoff = self.handoff
            ack_ready = self.ack_loaded
        if not handoff or not ack_ready:
            return
        for key in ('active', 'recovery'):
            row = handoff[key]
            if row is None:
                continue
            field = 'active' if key == 'active' else 'terminal'
            with self.lock:
                if self.ack[field] == row['episode_id'] or self.ack['terminal'] == row['episode_id']:
                    continue
            result = await self.alerts.record('daemon_loop_stalled', episode_id=row['episode_id'],
                                              condition=row['condition'], cause=row['cause'])
            if result is None:
                return  # unavailable durable sink; do not acknowledge lost work
            with self.lock:
                self.ack[field] = row['episode_id']
                self.ack_dirty = True

    async def run_consumer(self) -> None:
        while True:
            try:
                await self.consume_once()
            except Exception as exc:
                _diagnostic('daemon_progress handoff_retry=%s', type(exc).__name__)
            await asyncio.sleep(INTERVAL_S)

    async def stop(self) -> None:
        self.stop_event.set()
        if self.pulse_handle is not None:
            self.pulse_handle.cancel()
        if self.thread is not None:
            await asyncio.to_thread(self.thread.join, 2.0)
            if self.thread.is_alive():
                raise RuntimeError('publisher_stop_timeout')
            self.thread = None
