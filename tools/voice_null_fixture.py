"""Disposable capture fixture around the real speaker service, always null-only."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--speaker-root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ['MIC_SPEAKER_SINK'] = 'null'
    os.environ['MIC_SPEAKER_OUTPUT_DIR'] = str(output/'audio')
    sys.path.insert(0, str(Path(args.speaker_root).resolve()/'mic-server'))
    from speaker_service import SpeakerService
    from resident_speaker import ResidentSpeaker, NullSink, PlayerSink
    # Tripwire: this process can never accidentally reach the platform player.
    def forbidden(*args, **kwargs):
        raise AssertionError('An audio device was requested by the null fixture')
    PlayerSink.consume = forbidden
    events = []
    speaker = ResidentSpeaker(sink=NullSink())
    service = SpeakerService(speaker=speaker, emit=events.append)
    listener = SimpleNamespace(running=True, state='LISTENING', wake=SimpleNamespace(lock=threading.RLock()), until=0)
    listener.recognition_stamp = lambda: (0, time.monotonic() < listener.until)
    listener.suppress_recognition_until = lambda stamp: setattr(listener, 'until', stamp)
    pending = []
    requests = []
    generation = 'fixture-generation'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send_json(self, value, status=200):
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            try:
                self.wfile.write(json.dumps(value).encode())
            except BrokenPipeError:
                pass
        def do_GET(self):
            if self.path == '/status':
                self.send_json({'mode':'on','wake':{'enabled':True,'generation':generation,'pending_count':len(pending)},'speaker':service.status()})
            else:
                self.send_json({'error':'unsupported'},404)
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
            requests.append({'path':self.path,'data':data,'timestamp':time.time()})
            if self.path == '/fixture/capture':
                listener.until = 0  # Counterpart: a new completed capture follows the prior echo fence.
                metadata = service.capture_ended('room_mic', listener)
                pending.append({'id':data['id'],'generation':generation,'text':data['text'],**metadata})
                service.mark(metadata['conversation_id'], 'routed_at')
                result = {'ok':True}
            elif self.path == '/wake/claim':
                capture = pending.pop(0) if pending else None
                if capture:
                    capture['voice_reply'] = service.contract(capture['conversation_id'])
                    service.mark(capture['conversation_id'], 'claimed_at')
                result = {'claim':capture}
            elif self.path == '/conversation/timing':
                result = {'ok':service.mark(data.get('conversation_id'),data.get('stage'),data.get('at'))}
            elif self.path == '/speak':
                result = service.speak(data,listener)
            elif self.path == '/turn-ended':
                result = service.turn_ended(data.get('conversation_id'))
            else:
                result = {'error':'unsupported'}
            self.send_json(result)
            (output/'requests.json').write_text(json.dumps(requests,indent=2)+'\n')
            (output/'speaker-events.json').write_text(json.dumps(events,indent=2)+'\n')
    server = None
    try:
        service.start()
        if not service.ready:
            raise RuntimeError(service.error)
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        (output/'ready.json').write_text(json.dumps({'endpoint':f'http://127.0.0.1:{server.server_port}','pid':os.getpid(),'sink':'null','speaker':service.status()},indent=2)+'\n')
        def stop(*args):
            threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        server.serve_forever()
    finally:
        if server:
            server.server_close()
        service.close()
        (output/'cleanup.json').write_text(json.dumps({'http_closed':bool(server),'speaker_closed':not speaker.renderer.ready,'owned_processes_remaining':0},indent=2)+'\n')


if __name__ == '__main__':
    main()
