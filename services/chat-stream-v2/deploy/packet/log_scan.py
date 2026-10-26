"""Frozen byte ranges with bounded decoding; incomplete input never passes."""
from __future__ import annotations
import codecs
import os
from pathlib import Path

CHUNK_BYTES = 64 * 1024
MAX_RECORD_BYTES = 1024 * 1024

class IncompleteScan(ValueError):
    pass

def freeze(path, start=0, inode=None, device=None):
    path = Path(path)
    stat = path.stat()
    if start < 0 or stat.st_size < start or (inode is not None and stat.st_ino != inode) or (device is not None and stat.st_dev != device):
        raise IncompleteScan('log rotated/truncated or invalid boundary')
    return {'path': str(path), 'start_offset': start, 'end_offset': stat.st_size,
            'inode': stat.st_ino, 'device': stat.st_dev, 'bytes_read': stat.st_size-start}

def lines(item):
    yield from lines_many([item])

def lines_many(items):
    """Decode one frozen stream, including records split between generations."""
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    pending = ''
    items = list(items)
    for item in items:
        path = Path(item['path'])
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (item['inode'], item['device']):
                raise IncompleteScan('log rotated before read')
            stream.seek(item['start_offset'])
            remaining = item['bytes_read']
            while remaining:
                chunk = stream.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise IncompleteScan('log truncated during read')
                remaining -= len(chunk)
                pending += decoder.decode(chunk, final=False)
                pieces = pending.split('\n')
                pending = pieces.pop()
                for line in pieces:
                    if len(line.encode('utf-8')) + 1 > MAX_RECORD_BYTES:
                        raise IncompleteScan('record exceeds resource limit')
                    yield line + '\n'
                if len(pending.encode('utf-8')) > MAX_RECORD_BYTES:
                    raise IncompleteScan('record exceeds resource limit')
            closed = os.fstat(stream.fileno())
            if closed.st_size < item['end_offset']:
                raise IncompleteScan('log truncated during read')
    pending += decoder.decode(b'', final=True)
    if pending:
        raise IncompleteScan('unfinished record at frozen end')
    for item in items:
        path = Path(item['path'])
        current = path.stat()
        if (current.st_ino, current.st_dev) != (item['inode'], item['device']) or current.st_size < item['end_offset']:
            raise IncompleteScan('log rotated/truncated during read')


BOOT_PREFIX = 'chat_streamd_v2 listening on'


class AttributionAmbiguous(ValueError):
    """Complete bytes can still have unproven process provenance."""


def _slice_ranges(items, begin, end):
    """Retain exact inode-bound slices, including a record split by rotation."""
    result = []
    position = 0
    for item in items:
        size = item['bytes_read']
        left, right = max(begin, position), min(end, position + size)
        if right > left:
            start = item['start_offset'] + left - position
            result.append({**item, 'start_offset': start,
                           'end_offset': start + right - left, 'bytes_read': right - left})
        position += size
    return result


def generation_ranges(groups, expected_pid, expected_start):
    """Select only bytes after one matching, paired process-generation marker.

    Lifecycle ordering must be qualified separately on the target service
    manager. Timestamps and connection age never establish this boundary.
    """
    import json
    import uuid
    if type(expected_pid) is not int or expected_pid <= 0 or not isinstance(expected_start, str) or not expected_start.strip():
        raise AttributionAmbiguous('missing activated PID/start identity')
    expected_start = ' '.join(expected_start.split())
    if len(groups) != 2:
        raise AttributionAmbiguous('both stdout and stderr generation proofs are required')
    selected, preceding, markers = [], [], []
    for group in groups:
        position = 0
        matching = []
        boot_after = False
        for line in lines_many(group):
            size = len(line.encode('utf-8'))
            if BOOT_PREFIX in line:
                if matching:
                    boot_after = True
                try:
                    if not line.startswith(BOOT_PREFIX + ' '):
                        raise ValueError('invalid boot prefix')
                    identity = json.loads(line.split(' generation=', 1)[1])
                    if (not isinstance(identity, dict) or type(identity.get('pid')) is not int
                            or not isinstance(identity.get('start'), str)
                            or not identity['start'].strip()
                            or str(uuid.UUID(identity['boot_uuid'])) != identity['boot_uuid']):
                        raise ValueError('invalid generation identity')
                except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
                    raise AttributionAmbiguous('boot marker lacks valid PID/start/UUID proof') from exc
                if identity['pid'] == expected_pid:
                    if ' '.join(identity['start'].split()) != expected_start:
                        raise AttributionAmbiguous('boot marker PID/start mismatch')
                    matching.append((position, position + size, identity))
            position += size
        if len(matching) != 1 or boot_after:
            raise AttributionAmbiguous('missing, duplicate or out-of-order activated boot marker')
        begin, end, identity = matching[0]
        preceding.append(_slice_ranges(group, 0, begin))
        selected.append(_slice_ranges(group, end, position))
        markers.append({'identity': identity, 'inputs': _slice_ranges(group, begin, end)})
    if markers[0]['identity'] != markers[1]['identity']:
        raise AttributionAmbiguous('stdout/stderr generation identities conflict')
    return selected, preceding, markers
