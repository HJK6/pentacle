"""Resident synthesis and an explicit sink; null is the default."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import threading
import time
import uuid
import wave


class NullSink:
    name = 'null'

    def consume(self, path, deadline):
        # Reading the actual WAV proves rendered frames without an audio API.
        with wave.open(str(path), 'rb') as wav:
            if not wav.getnframes():
                raise ValueError('Rendered file has no frames')
            return dict(path=str(path), duration=wav.getnframes()/wav.getframerate())


class PlayerSink:
    name = 'player'

    def consume(self, path, deadline):
        subprocess.run(['afplay', str(path)], check=True,
                       timeout=max(.01, deadline-time.time()))
        return dict(path=str(path))


class KokoroWorker:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir).resolve()
        self.child = None
        self.ready = False
        self.model_loads = 0
        self.rss_bytes = None
        self.configuration = {}
        self.lock = threading.RLock()
        self.stderr = None

    def start(self):
        with self.lock:
            if self.child is not None:
                if self.child.poll() is not None:
                    raise RuntimeError('Resident synthesis process exited; restart is required')
                return
            python = os.environ.get('MIC_KOKORO_PYTHON', '')
            if not Path(python).is_absolute():
                raise ValueError('MIC_KOKORO_PYTHON must be an absolute interpreter path')
            for key in ('MIC_KOKORO_MODEL', 'MIC_KOKORO_VOICES'):
                if not Path(os.environ.get(key, '')).is_file():
                    raise ValueError(key+' must name a provisioned file')
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.stderr = (self.output_dir/'worker.log').open('ab')
            env = dict(os.environ, MIC_SPEAKER_OUTPUT_DIR=str(self.output_dir))
            self.child = subprocess.Popen([python, '-u', str(Path(__file__).with_name('resident_synth.py'))],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=self.stderr, env=env, text=True, bufsize=1)
            try:
                receipt = self._read(120)
                if not receipt.get('ready'):
                    raise RuntimeError('Resident speaker did not report ready')
                self.ready = True
                self.model_loads = receipt['model_loads']
                self.rss_bytes = receipt.get('rss_bytes')
                self.configuration = {key:receipt.get(key) for key in ('threads','execution_provider','priority')}
            except Exception:
                self.close()
                raise

    def _read(self, timeout):
        readable, _, _ = select.select([self.child.stdout], [], [], timeout)
        if not readable:
            self.close()
            raise TimeoutError('Resident synthesis timed out')
        line = self.child.stdout.readline()
        if not line:
            self.ready = False
            raise RuntimeError('Resident synthesis process exited')
        receipt = json.loads(line)
        if receipt.get('error'):
            raise RuntimeError(receipt['error'])
        return receipt

    def render(self, text, deadline):
        with self.lock:
            self.start()
            self.child.stdin.write(json.dumps(dict(text=text))+'\n')
            self.child.stdin.flush()
            receipt = self._read(max(.01, deadline-time.time()))
            path = Path(receipt['path']).resolve()
            if path.parent != self.output_dir or not path.is_file():
                raise RuntimeError('Worker returned an unowned file')
            self.rss_bytes = receipt.get('rss_bytes')
            self._prune(path)
            return receipt

    def _prune(self, current):
        # Only owned UUID WAVs; clip manifests/logs and caller files are excluded.
        files = sorted((p for p in self.output_dir.glob('*.wav')
                        if re.fullmatch(r'[0-9a-f]{32}', p.stem)), key=lambda p: p.stat().st_mtime)
        for index, path in enumerate(files):
            if path != current and (index < len(files)-256 or path.stat().st_mtime < time.time()-86400):
                path.unlink()

    def close(self):
        with self.lock:
            if self.child:
                if self.child.poll() is None:
                    self.child.terminate()
                    try:
                        self.child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.child.kill()
                        self.child.wait(timeout=3)
                for stream in (self.child.stdin, self.child.stdout):
                    if stream:
                        stream.close()
                self.child = None
            if self.stderr:
                self.stderr.close()
                self.stderr = None
            self.ready = False


class ResidentSpeaker:
    def __init__(self, renderer=None, sink=None, output_dir=None):
        self.output_dir = Path(output_dir or os.environ.get('MIC_SPEAKER_OUTPUT_DIR',
                                  str(Path(tempfile.gettempdir())/'pentacle-speaker'))).resolve()
        self.renderer = renderer or KokoroWorker(self.output_dir)
        self.sink = sink or (PlayerSink() if os.environ.get('MIC_SPEAKER_SINK', 'null') == 'player' else NullSink())
        self.lock = threading.Lock()
        self.last = None
        self.error = None

    def start(self):
        self.renderer.start()

    def speak(self, text, deadline, on_first_frame=None):
        if not isinstance(text, str) or not text.strip() or not 0 < len(text) <= 800:
            raise ValueError('Spoken response is too long')
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s]
        with self.lock, ThreadPoolExecutor(max_workers=1) as player:
            started = time.monotonic()
            receipts = []
            playing = None
            first = None
            for sentence in sentences:
                rendered = self.renderer.render(sentence, deadline)
                if playing:
                    playing.result()
                if first is None:
                    first = time.monotonic()-started
                    if on_first_frame:
                        on_first_frame()
                playing = player.submit(self.sink.consume, rendered['path'], deadline)
                receipts.append(rendered)
            playing.result()
            self.last = dict(stopped=True, played=self.sink.name == 'player', rendered=True,
                             sink=self.sink.name, first_frame_seconds=first,
                             elapsed_seconds=time.monotonic()-started,
                             duration=sum(r['duration'] for r in receipts), files=receipts)
            return self.last

    def clip(self, path, deadline, on_start=None):
        with self.lock:
            started = time.monotonic()
            if on_start:
                on_start()
            result = self.sink.consume(path, deadline)
            self.last = dict(stopped=True, rendered=True, played=self.sink.name == 'player',
                             sink=self.sink.name, first_frame_seconds=time.monotonic()-started, **result)
            return self.last

    def snapshot(self):
        return dict(ready=self.renderer.ready, model_loads=self.renderer.model_loads,
                    rss_bytes=getattr(self.renderer, 'rss_bytes', None), sink=self.sink.name, error=self.error,
                    renderer_pid=getattr(getattr(self.renderer, 'child', None), 'pid', None),
                    configuration=getattr(self.renderer, 'configuration', {}))

    def close(self):
        self.renderer.close()


class ClipBank:
    def __init__(self, speaker, root):
        self.speaker = speaker
        self.root = Path(root)
        self.manifest = {}
        self.previous = {}
        self.lock = threading.Lock()

    def load(self, clips):
        manifest = json.loads((self.root/'manifest.json').read_text())
        if manifest['voice'] != os.environ.get('MIC_KOKORO_VOICE', 'bm_george'):
            raise ValueError('Clip voice differs from configured voice')
        for group, phrases in clips.items():
            for phrase in phrases:
                entry = manifest['clips'][phrase]
                path = (self.root/entry['file']).resolve()
                if path.parent != self.root.resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                    raise ValueError('Clip file or digest mismatch')
        self.manifest = manifest

    def choose(self, group, phrases):
        with self.lock:
            options = [s for s in phrases if s != self.previous.get(group)] or phrases
            # Rotation is deterministic, useful in captured evidence, and never repeats a multi-clip set.
            previous = self.previous.get(group)
            selected = phrases[(phrases.index(previous)+1) % len(phrases)] if previous in phrases and len(phrases)>1 else options[0]
            self.previous[group] = selected
            return selected

    def play(self, group, phrases, deadline, on_start=None):
        selected = self.choose(group, phrases)
        entry = self.manifest['clips'][selected]
        path = self.root/entry['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Clip digest mismatch')
        return dict(text=selected, **self.speaker.clip(path, deadline, on_start=on_start))


def render_clips(speaker, root, clips):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest = dict(voice=os.environ.get('MIC_KOKORO_VOICE', 'bm_george'), clips={})
    for phrase in dict.fromkeys(s for phrases in clips.values() for s in phrases):
        result = speaker.renderer.render(phrase, time.time()+60)
        source = Path(result['path'])
        filename = hashlib.sha256(phrase.encode()).hexdigest()[:16]+'.wav'
        destination = root/filename
        destination.write_bytes(source.read_bytes())
        manifest['clips'][phrase] = dict(file=filename, text=phrase, sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
        if source != destination:
            source.unlink()
    temporary = root/('manifest-'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(manifest, indent=2))
    temporary.replace(root/'manifest.json')
    return manifest
