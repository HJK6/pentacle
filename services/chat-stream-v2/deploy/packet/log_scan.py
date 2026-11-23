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


def generation_ranges(groups, expected_pid, expected_start, *, allow_rotated_stderr=False):
    """Select only bytes from the paired activated generation markers.

    Lifecycle ordering must be qualified separately on the target service
    manager. Retained markers from other valid generations bound separate
    ranges; timestamps and connection age never establish this boundary.
    Manual recovery may prove that the entire retained stderr history
    postdates the boot. A marker-free stderr then contributes no bytes.
    """
    import json
    import uuid
    if type(expected_pid) is not int or expected_pid <= 0 or not isinstance(expected_start, str) or not expected_start.strip():
        raise AttributionAmbiguous('missing activated PID/start identity')
    expected_start = ' '.join(expected_start.split())
    if len(groups) != 2:
        raise AttributionAmbiguous('both stdout and stderr generation proofs are required')
    selected, preceding, markers = [], [], []
    for stream, group in enumerate(groups):
        position = 0
        boot_markers = []
        for line in lines_many(group):
            size = len(line.encode('utf-8'))
            if BOOT_PREFIX in line:
                if line.startswith(BOOT_PREFIX + ' '):
                    # Legacy history cannot identify a process generation.
                    identity = None
                else:
                    try:
                        if not line.startswith('generation='):
                            raise ValueError('missing generation prefix')
                        identity, marker_end = json.JSONDecoder().raw_decode(line[len('generation='):])
                        if not line[len('generation=') + marker_end:].startswith(' ' + BOOT_PREFIX + ' '):
                            raise ValueError('invalid listening record')
                        if (not isinstance(identity, dict) or type(identity.get('pid')) is not int
                                or not isinstance(identity.get('start'), str)
                                or not identity['start'].strip()
                                or str(uuid.UUID(identity['boot_uuid'])) != identity['boot_uuid']):
                            raise ValueError('invalid generation identity')
                    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
                        raise AttributionAmbiguous('boot marker lacks valid PID/start/UUID proof') from exc
                boot_markers.append((position, position + size, identity))
            position += size
        matching = [row for row in boot_markers if row[2] is not None
                    and row[2]['pid'] == expected_pid
                    and ' '.join(row[2]['start'].split()) == expected_start]
        if stream == 1 and allow_rotated_stderr and not boot_markers:
            selected.append([])
            preceding.append(group)
            continue
        if len(matching) != 1:
            raise AttributionAmbiguous('missing or duplicate activated boot marker')
        begin, end, identity = matching[0]
        if any(marker_begin > begin and marker_identity is None
               for marker_begin, _, marker_identity in boot_markers):
            raise AttributionAmbiguous('unqualified boot marker follows activated marker')
        later_generations = [marker_begin for marker_begin, _, marker_identity in boot_markers
                             if marker_begin >= end and marker_identity is not None]
        boundary = min(later_generations) if later_generations else position
        preceding.append(_slice_ranges(group, 0, begin) + _slice_ranges(group, boundary, position))
        selected.append(_slice_ranges(group, end, boundary))
        markers.append({'identity': identity, 'inputs': _slice_ranges(group, begin, end)})
    if len(markers) == 2 and markers[0]['identity'] != markers[1]['identity']:
        raise AttributionAmbiguous('stdout/stderr generation identities conflict')
    return selected, preceding, markers
