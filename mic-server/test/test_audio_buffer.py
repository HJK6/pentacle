import json
import time
import wave
from pathlib import Path

import numpy as np
import pytest


def test_records_original_pcm_and_preserves_across_expiry(tmp_path):
    from audio_buffer import AudioBuffer
    now = [100000.0]
    buffer = AudioBuffer(tmp_path, sample_rate=4, chunk_seconds=1, clock=lambda: now[0])
    try:
        buffer.submit(np.array([-1., -.5, 0., .5]), started_at=now[0])
        buffer.flush()
        files = list((tmp_path/'rolling').glob('*.wav'))
        assert len(files) == 1
        with wave.open(str(files[0]), 'rb') as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (4, 1, 2, 4)
            assert np.frombuffer(audio.readframes(4), dtype='<i2').tolist() == [-32768, -16384, 0, 16384]
        saved = buffer.keep(100000, 100001, 'Wake phrase was missed')
        manifest = json.loads(Path(saved['manifest']).read_text())
        assert manifest['feedback'] == 'Wake phrase was missed'
        assert manifest['coverage_complete'] is True
        now[0] += 86402
        buffer.cleanup()
        assert not list((tmp_path/'rolling').glob('*.wav'))
        assert list(Path(saved['directory']).glob('*.wav'))
    finally:
        buffer.close()


def wait_for(predicate, timeout=3):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_full_window_startup_cleanup_unknown_and_crash_tails(tmp_path):
    from audio_buffer import AudioBuffer
    now = [100000.]
    buffer = AudioBuffer(tmp_path, sample_rate=4, chunk_seconds=1, clock=lambda: now[0])
    buffer.submit(np.zeros(8), started_at=100000)
    buffer.flush()
    buffer.close()
    files = sorted((tmp_path/'rolling').glob('*.wav'))
    unknown = tmp_path/'rolling'/'notes.wav'
    unknown.write_bytes(b'keep me')
    malformed = tmp_path/'rolling'/('chunk-1-'+'a'*32+'.wav.json')
    malformed.write_text('{}')
    # At exactly the trailing boundary, retain the chunk crossing the boundary.
    now[0] = 100001.5+86400
    buffer = AudioBuffer(tmp_path, sample_rate=4, chunk_seconds=1, clock=lambda: now[0])
    try:
        assert not files[0].exists()
        assert files[1].exists()
        assert unknown.exists() and malformed.exists()
        # Simulate crash in final rename/metadata publication boundary.
        meta_path = Path(str(files[1])+'.json')
        meta = json.loads(meta_path.read_text())
        meta['state'] = 'writing'
        meta_path.write_text(json.dumps(meta))
        assert buffer._entries() == []
        now[0] += 2
        buffer.cleanup()
        assert not files[1].exists() and not meta_path.exists()
        assert unknown.exists() and malformed.exists()
    finally:
        buffer.close()


def test_worker_expires_after_off_without_new_audio(tmp_path):
    from audio_buffer import AudioBuffer
    now = [100000.]
    buffer = AudioBuffer(tmp_path, sample_rate=4, clock=lambda: now[0], cleanup_seconds=.03)
    try:
        buffer.submit(np.zeros(4), started_at=100000)
        buffer.flush()  # Same boundary used by Mic Off; writer keeps running.
        assert len(list(buffer.rolling.glob('*.wav'))) == 1
        now[0] += 86402
        wait_for(lambda: not list(buffer.rolling.glob('*.wav')))
        assert buffer.worker.is_alive()
    finally:
        buffer.close()


def test_symlink_and_unowned_metadata_are_never_deleted(tmp_path):
    from audio_buffer import AudioBuffer, FORMAT
    outside = tmp_path/'outside.wav'
    outside.write_bytes(b'precious')
    root = tmp_path/'buffer'
    buffer = AudioBuffer(root, clock=lambda: 999999)
    try:
        name = 'chunk-1-'+'a'*32+'.wav'
        link = buffer.rolling/name
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip('Host does not permit symlink creation')
        sidecar = buffer.rolling/(name+'.json')
        sidecar.write_text(json.dumps(dict(format=FORMAT, state='complete', name=name, started_at=0, ended_at=1)))
        buffer.cleanup()
        assert link.is_symlink() and sidecar.exists() and outside.read_bytes() == b'precious'
        bad = buffer.rolling/('chunk-2-'+'b'*32+'.wav.json')
        bad.write_text(json.dumps(dict(format=FORMAT, state='complete', name='../outside.wav', started_at=0, ended_at=1)))
        buffer.cleanup()
        assert bad.exists()
    finally:
        buffer.close()
    linked_root = tmp_path/'linked-root'
    linked_root.symlink_to(root, target_is_directory=True)
    refused = AudioBuffer(linked_root)
    assert not refused.ready and 'symlink' in refused.last_error


def test_partial_keep_records_real_gaps_and_no_audio_errors(tmp_path):
    from audio_buffer import AudioBuffer
    buffer = AudioBuffer(tmp_path, sample_rate=4, chunk_seconds=1)
    try:
        buffer.submit(np.zeros(4), started_at=100)
        buffer.submit(np.zeros(4), started_at=103)
        result = buffer.keep(99, 105)
        manifest = json.loads(Path(result['manifest']).read_text())
        assert manifest['gaps'] == [[99, 100], [101, 103], [104, 105]]
        assert result['coverage_complete'] is False
        with pytest.raises(FileNotFoundError):
            buffer.keep(10, 20)
        for start, end in [(None, 2), (1, 1), (float('nan'), 5)]:
            with pytest.raises(ValueError):
                buffer.start_keep(start, end)
    finally:
        buffer.close()


def test_queue_overflow_is_nonblocking_and_gap_observable(tmp_path, monkeypatch):
    import threading
    from audio_buffer import AudioBuffer
    entered, release = threading.Event(), threading.Event()
    buffer = AudioBuffer(tmp_path, sample_rate=4, chunk_seconds=1, queue_size=1)
    original = buffer._write
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        original(*args)
    monkeypatch.setattr(buffer, '_write', blocked)
    try:
        buffer.submit(np.zeros(4), started_at=100)
        assert entered.wait(1)
        buffer.submit(np.zeros(4), started_at=101)
        start = time.monotonic()
        buffer.submit(np.zeros(4), started_at=102)
        assert time.monotonic()-start < .2
        assert buffer.snapshot()['dropped_frames'] == 4
        release.set()
        buffer.flush()
        buffer.submit(np.zeros(4), started_at=103)
        buffer.flush()
        result = buffer.keep(100, 104)
        manifest = json.loads(Path(result['manifest']).read_text())
        assert manifest['gaps'] == [[102, 103]]
        assert manifest['chunks'][-1]['gap_before_frames'] == 4
    finally:
        release.set()
        buffer.close()


def test_disk_failure_does_not_escape_callback(tmp_path, monkeypatch):
    from audio_buffer import AudioBuffer
    buffer = AudioBuffer(tmp_path, sample_rate=4)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(buffer, '_metadata', fail)
    try:
        buffer.submit(np.zeros(4), started_at=100)
        buffer.flush()
        assert buffer.snapshot()['last_error'] == 'disk full'
        assert buffer.snapshot()['dropped_frames'] == 4
        assert buffer.worker.is_alive()
    finally:
        buffer.close()


def test_copy_pins_expired_chunks_without_blocking_recording(tmp_path, monkeypatch):
    import audio_buffer
    import threading
    now = [100000.]
    buffer = audio_buffer.AudioBuffer(tmp_path, sample_rate=4, chunk_seconds=1, clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    copy = audio_buffer.shutil.copyfile
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return copy(*args)
    monkeypatch.setattr(audio_buffer.shutil, 'copyfile', blocked)
    try:
        buffer.submit(np.zeros(4), started_at=100000)
        job = buffer.start_keep(100000, 100001)
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match='already running'):
            buffer.start_keep(100000, 100001)
        now[0] += 86402
        buffer.cleanup()
        assert list(buffer.rolling.glob('*.wav'))
        buffer.submit(np.zeros(4), started_at=now[0])
        buffer.flush()
        assert len(list(buffer.rolling.glob('*.wav'))) == 2
        release.set()
        wait_for(lambda: buffer.snapshot()['keep']['state'] != 'running')
        assert buffer.snapshot()['keep']['id'] == job
        assert buffer.snapshot()['keep']['state'] == 'complete'
        assert not buffer.pinned
        buffer.cleanup()
        assert len(list(buffer.rolling.glob('*.wav'))) == 1
    finally:
        release.set()
        buffer.close()


def test_failed_copy_removes_only_own_partial_saved_files(tmp_path, monkeypatch):
    import audio_buffer
    buffer = audio_buffer.AudioBuffer(tmp_path, sample_rate=4)
    def fail(source, target):
        Path(target).write_bytes(b'partial')
        raise OSError('copy failed')
    monkeypatch.setattr(audio_buffer.shutil, 'copyfile', fail)
    try:
        buffer.submit(np.zeros(4), started_at=100)
        with pytest.raises(OSError, match='copy failed'):
            buffer.keep(100, 101)
        assert not list(buffer.saved.iterdir()) and not buffer.pinned
        existing = buffer.saved/('a'*32)
        existing.mkdir()
        with pytest.raises(FileExistsError):
            buffer.keep(100, 101, job_id='a'*32)
        assert existing.exists()
    finally:
        buffer.close()


def test_raw_callback_records_during_finalization_and_off_flushes(tmp_path, monkeypatch):
    import audio_buffer
    from .test_listener_lifecycle import install_listener_import_fakes, import_fresh_module
    install_listener_import_fakes(monkeypatch)
    buffer = audio_buffer.AudioBuffer(tmp_path)
    monkeypatch.setattr(audio_buffer, 'get_audio_buffer', lambda: buffer)
    module = import_fresh_module('always_on')
    listener = module.AlwaysOnListener()
    monkeypatch.setattr(listener, '_emit_callback_health', lambda *args: None)
    listener.running = True
    listener._finishing_capture = True
    try:
        listener._audio_callback(np.full((480, 1), .25, dtype=np.float32), 480, None, None)
        listener.stop()
        files = list(buffer.rolling.glob('*.wav'))
        assert len(files) == 1
        with wave.open(str(files[0])) as audio:
            assert audio.getframerate() == 48000 and audio.getnframes() == 480
            assert set(np.frombuffer(audio.readframes(480), dtype='<i2')) == {8192}
        listener._audio_callback(np.zeros((480, 1)), 480, None, None)
        assert buffer.seen_frames == 480
        assert buffer.worker.is_alive()
    finally:
        buffer.close()


def test_single_thread_http_off_and_status_work_during_copy(tmp_path, monkeypatch):
    import audio_buffer
    import mic_server
    import threading
    from http.server import HTTPServer
    from urllib.request import Request, urlopen
    buffer = audio_buffer.AudioBuffer(tmp_path, sample_rate=4)
    entered, release = threading.Event(), threading.Event()
    original = audio_buffer.shutil.copyfile
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(audio_buffer.shutil, 'copyfile', blocked)
    monkeypatch.setattr(mic_server, 'get_audio_buffer', lambda: buffer)
    monkeypatch.setattr(mic_server, 'stop_all', lambda: None)
    monkeypatch.setattr(mic_server, 'always_on_listener', None)
    monkeypatch.setattr(mic_server, 'state', dict(mic_server.state, mode='off', last_error=None))
    server = HTTPServer(('127.0.0.1', 0), mic_server.MicHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, body=None):
        req = Request(f'http://127.0.0.1:{server.server_port}'+path,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=1) as response:
            return response.status, json.load(response)
    try:
        buffer.submit(np.zeros(4), started_at=100)
        status, admitted = request('/audio/keep', {'start': 100, 'end': 101})
        assert status == 202 and entered.wait(1)
        assert request('/status')[1]['audio_buffer']['keep']['state'] == 'running'
        assert request('/mode/off', {})[0] == 200
        assert request('/status')[1]['mode'] == 'off'
        release.set()
        wait_for(lambda: buffer.snapshot()['keep']['state'] == 'complete')
        assert buffer.snapshot()['keep']['id'] == admitted['id']
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        buffer.close()


def test_disabled_default_and_cli_rejects_ambiguous_ranges(monkeypatch):
    import audio_buffer
    import audio_buffer_cli
    monkeypatch.delenv('MIC_AUDIO_BUFFER_DIR', raising=False)
    assert audio_buffer.get_audio_buffer() is None
    for args in [['keep'], ['keep', '--last-seconds', '-1'], ['keep', '--start', '2026-09-12T13:00:00']]:
        with pytest.raises(SystemExit):
            audio_buffer_cli.main(args)


def test_cli_waits_for_its_single_job_and_prints_saved_path(monkeypatch, capsys):
    import audio_buffer_cli as cli
    calls = []
    def request(path, body=None):
        calls.append((path, body))
        if path == '/audio/keep':
            assert body['end']-body['start'] == 60
            return {'id': 'one'}
        return {'audio_buffer': {'keep': {'id': 'one', 'state': 'complete', 'result': {'directory': '/local/saved/one'}}}}
    monkeypatch.setattr(cli, 'request', request)
    cli.main(['keep', '--last-seconds', '60', '--feedback', 'missed wake'])
    assert '/local/saved/one' in capsys.readouterr().out
    assert sum(path == '/audio/keep' for path, _ in calls) == 1

