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
    path = Path(item['path'])
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    pending = ''
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
        pending += decoder.decode(b'', final=True)
        if pending:
            # A frozen end through a partial record cannot establish its meaning.
            raise IncompleteScan('unfinished record at frozen end')
        closed = os.fstat(stream.fileno())
        current = path.stat()
        if (current.st_ino, current.st_dev) != (item['inode'], item['device']) or closed.st_size < item['end_offset'] or current.st_size < item['end_offset']:
            raise IncompleteScan('log rotated/truncated during read')
