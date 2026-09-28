"""Inspect the local buffer or preserve a time range without replaying speech."""
import argparse
from datetime import datetime
import json
import math
import time
from urllib.request import Request, urlopen


def utc_seconds(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError('Include a UTC offset or Z')
    return parsed.timestamp()


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request('http://127.0.0.1:7780'+path, data=data, headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    keep = commands.add_parser('keep')
    keep.add_argument('--last-seconds', type=float)
    keep.add_argument('--start', type=utc_seconds)
    keep.add_argument('--end', type=utc_seconds)
    keep.add_argument('--feedback', default='')
    args = parser.parse_args(argv)
    if args.command == 'status':
        print(json.dumps(request('/status').get('audio_buffer', {'enabled': False}), indent=2))
        return
    if args.last_seconds is not None:
        if args.start is not None or args.end is not None or not math.isfinite(args.last_seconds) or args.last_seconds <= 0:
            parser.error('Use positive --last-seconds alone, or both --start and --end')
        end = time.time()
        start = end-args.last_seconds
    else:
        start, end = args.start, args.end
        if start is None or end is None or end <= start:
            parser.error('Specify --last-seconds or a valid --start/--end interval')
    admitted = request('/audio/keep', dict(start=start, end=end, feedback=args.feedback))
    deadline = time.monotonic()+300
    while time.monotonic() < deadline:
        job = request('/status')['audio_buffer']['keep']
        if not job or job['id'] != admitted['id']:
            raise RuntimeError('Keep status replaced; inspect saved directory before retrying')
        if job['state'] == 'error':
            raise RuntimeError(job['error'])
        if job['state'] == 'complete':
            print(json.dumps(job, indent=2))
            return
        time.sleep(.25)
    raise TimeoutError('Keep still running; inspect status before retrying')


if __name__ == '__main__':
    main()
