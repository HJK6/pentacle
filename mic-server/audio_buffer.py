"""Local rolling PCM audio and one background clip-preservation operation."""
import atexit
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import uuid
import wave

import numpy as np

FORMAT = 'pentacle.mic-audio.v1'
NAME = re.compile(r'^chunk-[0-9]+-[0-9a-f]{32}\.wav$')


class AudioBuffer:
    def __init__(self, root, sample_rate=48000, chunk_seconds=60, clock=time.time, queue_size=128, cleanup_seconds=60):
        self.root = Path(root).absolute()
        self.rolling, self.saved = self.root/'rolling', self.root/'saved'
        self.rate, self.max_frames, self.clock = sample_rate, sample_rate*chunk_seconds, clock
        self.cleanup_seconds = cleanup_seconds
        self.queue = queue.Queue(maxsize=queue_size)
        self.lock = threading.RLock()
        self.file_lock = threading.RLock()
        self.pinned = set()
        self.dropped_frames = self.seen_frames = 0
        self.last_error = None
        self.keep_job = None
        self.current = None
        self.expected_frame = None
        self.closed = False
        self.ready = False
        self.worker = None
        self.context = {key: os.environ.get(key) for key in ('MIC_RUNTIME_SOURCE', 'MIC_WHISPER_MODEL', 'MIC_WHISPER_DEVICE', 'MIC_WHISPER_COMPUTE_TYPE', 'MIC_WAKE_CAPTURE')}
        try:
            if self.root.is_symlink():
                raise ValueError('Audio buffer root cannot be a symlink')
            for directory in (self.rolling, self.saved):
                if directory.is_symlink():
                    raise ValueError('Audio buffer directories cannot be symlinks')
                directory.mkdir(parents=True, exist_ok=True)
            self.cleanup()
            self.ready = True
            self.worker = threading.Thread(target=self._run, name='mic-audio-buffer', daemon=True)
            self.worker.start()
        except Exception as exc:
            self.last_error = str(exc)

    def submit(self, samples, started_at=None):
        """No disk access or blocking waits in the microphone callback."""
        count = len(samples)
        first = self.seen_frames
        self.seen_frames += count
        if not self.ready or self.closed:
            self.dropped_frames += count
            return
        started_at = self.clock()-count/self.rate if started_at is None else started_at
        try:
            # The callback gives us its own mono array, not PortAudio's reused buffer.
            self.queue.put_nowait(('audio', (samples, started_at, first)))
        except queue.Full:
            self.dropped_frames += count
            self.last_error = 'Audio archive queue full; frames dropped'

    def flush(self):
        if not self.ready or self.closed:
            raise RuntimeError(self.last_error or 'Audio archive is not running')
        done = threading.Event()
        request = {'done': done, 'error': None}
        self.queue.put(('flush', request), timeout=5)
        if not done.wait(10):
            raise TimeoutError('Audio archive flush timed out')
        if request['error']:
            raise RuntimeError(request['error'])

    def _run(self):
        next_cleanup = time.monotonic()+self.cleanup_seconds
        while True:
            try:
                kind, value = self.queue.get(timeout=max(0.01, min(1, next_cleanup-time.monotonic())))
            except queue.Empty:
                kind, value = None, None
            try:
                if kind == 'audio':
                    self._write(*value)
                elif kind in ('flush', 'close'):
                    self._finish()
            except Exception as exc:
                self.last_error = str(exc)
                if kind == 'audio':
                    self.dropped_frames += len(value[0])
                if value is not None and kind in ('flush', 'close'):
                    value['error'] = str(exc)
                self._abandon()
            finally:
                if kind in ('flush', 'close'):
                    value['done'].set()
            if kind == 'close':
                return
            if time.monotonic() >= next_cleanup:
                try:
                    self.cleanup()
                except Exception as exc:
                    self.last_error = str(exc)
                next_cleanup = time.monotonic()+self.cleanup_seconds

    def _write(self, samples, started_at, first):
        gap = first if self.expected_frame is None else max(0, first-self.expected_frame)
        if self.current:
            meta = self.current[1]
            expected_at = meta['started_at']+meta['frames']/self.rate
            if abs(started_at-expected_at) > .1:
                self._finish()
        if gap:
            self._finish()
        pcm = np.clip(np.rint(np.nan_to_num(samples)*32768), -32768, 32767).astype('<i2')
        offset = 0
        while offset < len(pcm):
            if self.current is None:
                name = f'chunk-{int((started_at+offset/self.rate)*1000000)}-{uuid.uuid4().hex}.wav'
                meta = dict(format=FORMAT, state='writing', name=name, started_at=started_at+offset/self.rate,
                            sample_rate=self.rate, channels=1, sample_width=2, frames=0,
                            gap_before_frames=gap, context=self.context)
                self._metadata(name, meta)
                handle = wave.open(str(self.rolling/(name+'.part')), 'wb')
                handle.setparams((1, 2, self.rate, 0, 'NONE', 'not compressed'))
                self.current = (handle, meta)
                gap = 0
            handle, meta = self.current
            count = min(len(pcm)-offset, self.max_frames-meta['frames'])
            handle.writeframesraw(pcm[offset:offset+count].tobytes())
            meta['frames'] += count
            offset += count
            if meta['frames'] >= self.max_frames:
                self._finish()
        self.expected_frame = first+len(samples)

    def _metadata(self, name, metadata):
        temp = self.rolling/(name+'.json.tmp')
        temp.write_text(json.dumps(metadata, sort_keys=True)+'\n', encoding='utf-8')
        temp.replace(self.rolling/(name+'.json'))

    def _finish(self):
        if self.current is None:
            return
        handle, meta = self.current
        handle.close()
        name = meta['name']
        part = self.rolling/(name+'.part')
        with part.open('rb') as source:
            meta['sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
        meta.update(state='complete', ended_at=meta['started_at']+meta['frames']/self.rate)
        with self.file_lock:
            part.replace(self.rolling/name)
            self._metadata(name, meta)
        self.current = None

    def _abandon(self):
        if self.current:
            try:
                self.current[0].close()
            except Exception:
                pass
            self.current = None

    def _entries(self, partial=False):
        entries = []
        if self.root.is_symlink() or self.rolling.is_symlink():
            raise ValueError('Audio buffer path became a symlink')
        for sidecar in self.rolling.glob('chunk-*.wav.json'):
            name = sidecar.name[:-5]
            if not NAME.fullmatch(name) or sidecar.is_symlink():
                continue
            try:
                meta = json.loads(sidecar.read_text(encoding='utf-8'))
                if not isinstance(meta, dict):
                    continue
                if meta.get('format') != FORMAT or meta.get('name') != name:
                    continue
                complete = meta.get('state') == 'complete'
                if not complete and not (partial and meta.get('state') == 'writing'):
                    continue
                path = self.rolling/(name if complete else name+'.part')
                # A crash after rename but before metadata publication leaves a
                # writing sidecar with a .wav: expire it, never offer it as complete.
                if not complete and not path.exists():
                    path = self.rolling/name
                if path.is_symlink() or not path.is_file():
                    continue
                start = meta.get('started_at')
                end = meta.get('ended_at') if complete else start+self.max_frames/self.rate
                if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end) or end < start:
                    continue
                entries.append((path, sidecar, meta, end))
            except (ValueError, TypeError, OSError):
                continue
        return sorted(entries, key=lambda entry: entry[2]['started_at'])

    def cleanup(self):
        cutoff = self.clock()-86400
        with self.file_lock:
            for audio, metadata, info, end in self._entries(partial=True):
                if end <= cutoff and info['name'] not in self.pinned and (not self.current or info['name'] != self.current[1]['name']):
                    audio.unlink()
                    metadata.unlink(missing_ok=True)

    def keep(self, start, end, feedback='', job_id=None):
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end)) or end <= start:
            raise ValueError('Require finite UTC start/end seconds with end after start')
        self.flush()
        with self.file_lock:
            entries = [item for item in self._entries() if item[2]['started_at'] < end and item[3] > start]
            if not entries:
                raise FileNotFoundError('No recorded audio covers this interval')
            self.pinned.update(item[2]['name'] for item in entries)
        destination = self.saved/(job_id or uuid.uuid4().hex)
        created = []
        made_directory = False
        try:
            if self.root.is_symlink() or self.saved.is_symlink():
                raise ValueError('Saved audio path became a symlink')
            if job_id is not None and not re.fullmatch(r'[0-9a-f]{32}', job_id):
                raise ValueError('Invalid keep operation ID')
            destination.mkdir(exist_ok=False)
            made_directory = True
            clips = []
            cursor, gaps = start, []
            for audio, _, info, actual_end in entries:
                target = destination/audio.name
                created.append(target)
                shutil.copyfile(audio, target)
                with target.open('rb') as saved:
                    digest = hashlib.file_digest(saved, 'sha256').hexdigest()
                if digest != info['sha256']:
                    raise ValueError('Recorded chunk checksum mismatch')
                a, b = max(start, info['started_at']), min(end, actual_end)
                if a > cursor+0.05:
                    gaps.append([cursor, a])
                cursor = max(cursor, b)
                clips.append(dict(info, saved_file=target.name))
            if cursor < end-0.05:
                gaps.append([cursor, end])
            manifest = dict(format=FORMAT, requested_start=start, requested_end=end,
                            feedback=str(feedback)[:2000], coverage_complete=not gaps, gaps=gaps,
                            chunks=clips, saved_at=self.clock())
            path = destination/'manifest.json'
            created.append(path)
            path.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
            return {'directory': str(destination), 'manifest': str(path), 'chunks': len(clips), 'coverage_complete': not gaps}
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            if made_directory:
                destination.rmdir()
            raise
        finally:
            with self.file_lock:
                self.pinned.difference_update(item[2]['name'] for item in entries)

    def start_keep(self, start, end, feedback=''):
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end)) or end <= start:
            raise ValueError('Require finite UTC start/end seconds with end after start')
        with self.lock:
            if self.keep_job and self.keep_job['state'] == 'running':
                raise RuntimeError('A keep operation is already running')
            job_id = uuid.uuid4().hex
            self.keep_job = dict(id=job_id, state='running')
        def run():
            try:
                result = self.keep(start, end, feedback, job_id)
                job = dict(id=job_id, state='complete', result=result)
            except Exception as exc:
                job = dict(id=job_id, state='error', error=str(exc))
            with self.lock:
                self.keep_job = job
        threading.Thread(target=run, name='mic-audio-keep', daemon=True).start()
        return job_id

    def snapshot(self):
        with self.lock:
            return dict(enabled=True, ready=self.ready and not self.closed, root=str(self.root), retention_hours=24,
                        sample_rate=self.rate, channels=1, sample_width=2, dropped_frames=self.dropped_frames,
                        queued_blocks=self.queue.qsize(), last_error=self.last_error,
                        keep=dict(self.keep_job) if self.keep_job else None)

    def close(self):
        if self.closed:
            return
        if self.worker and self.worker.is_alive():
            request = {'done': threading.Event(), 'error': None}
            try:
                self.queue.put(('close', request), timeout=5)
                request['done'].wait(10)
                self.worker.join(timeout=1)
            except queue.Full:
                self.last_error = 'Audio writer could not close promptly'
        self.closed = True


_instance = None


def get_audio_buffer():
    global _instance
    root = os.environ.get('MIC_AUDIO_BUFFER_DIR')
    if not root:
        return None
    if _instance is None:
        _instance = AudioBuffer(root)
        atexit.register(_instance.close)
    return _instance
