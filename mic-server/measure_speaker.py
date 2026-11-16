"""Measure real resident synthesis/clip latency with a null sink; no microphone or player."""
import argparse
import json
import math
import os
from pathlib import Path
import platform
import threading
import time
from types import SimpleNamespace
from speaker_service import SpeakerService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--runs', type=int, default=20)
    args = parser.parse_args()
    if args.runs < 20 or os.environ.get('MIC_SPEAKER_SINK', 'null') != 'null':
        parser.error('At least twenty runs and the null sink are required')
    service = SpeakerService(emit=lambda _: None)
    # Counterpart is the capture listener; synthesis, clips and policy are real.
    listener = SimpleNamespace(running=True, state='LISTENING', wake=SimpleNamespace(lock=threading.RLock()), until=0)
    listener.recognition_stamp = lambda: (0, time.monotonic() < listener.until)
    listener.suppress_recognition_until = lambda stamp: setattr(listener, 'until', stamp)
    rows = []
    try:
        service.start()
        if not service.ready:
            raise RuntimeError(service.error)
        for index in range(args.runs):
            listener.until = 0
            recognised = time.monotonic()
            cid = service.open('room_mic', listener)
            ack = service.last
            ack_start = time.monotonic()-recognised
            listener.until = 0  # Captured output has finished; advance the fixture past the echo fence.
            accepted = time.monotonic()
            result = service.speak(dict(conversation_id=cid, kind='reply', text='It is ready. The detail is in chat.', final=True), listener)
            if result['outcome'] != 'spoken' or ack['outcome'] != 'spoken':
                raise RuntimeError(str(result))
            row = dict(index=index, ack_seconds=ack_start, first_frame_seconds=result['receipt']['first_frame_seconds'],
                       total_seconds=time.monotonic()-accepted, load=os.getloadavg(), receipt=result['receipt'])
            rows.append(row)
            print(json.dumps(dict(index=index, ack_seconds=ack_start, first_frame_seconds=row['first_frame_seconds'])), flush=True)
        def p95(key):
            return sorted(r[key] for r in rows)[math.ceil(.95*len(rows))-1]
        receipt = dict(runs=rows, p95_ack_seconds=p95('ack_seconds'), p95_first_frame_seconds=p95('first_frame_seconds'),
                       speaker=service.status(), platform=platform.platform(), interpreter=os.sys.executable,
                       measured_scope='room_mic claim policy to captured ack; accepted two-sentence line to real WAV; fixture listener, no device',
                       measured_at=time.time())
        Path(args.output).write_text(json.dumps(receipt, indent=2))
        print(json.dumps({k: receipt[k] for k in ('p95_ack_seconds','p95_first_frame_seconds')}), flush=True)
    finally:
        service.close()


if __name__ == '__main__':
    main()
