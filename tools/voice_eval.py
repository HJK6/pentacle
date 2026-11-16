"""Replay model-authored fixture transcripts against a fake or null speak sink."""
import argparse
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.voice_line import check_line, check_expects_answer
from tools.voice_reply import submit_line


def evaluate(cases, transcripts, endpoint):
    index = {item['id']: item for item in transcripts}
    results = []
    for case in cases:
        transcript = index[case['id']]
        events = transcript['events']
        recorded = []
        for event in events:
            if event['type'] == 'speech':
                text = event['text']
                expects_answer = bool(event.get('expects_answer', False))
                record = {**event, 'checker_reason': check_line(text),
                          'expects_answer_reason': check_expects_answer(text) if expects_answer else None}
                record['outcome'] = submit_line(case['conversation_id'], 'reply', text,
                                                final=event.get('final', False), endpoint=endpoint,
                                                expects_answer=expects_answer)
            else:
                # Fixture delegation is the injected counterpart. Preserve the
                # actor's chosen event order; never insert or reorder a kickoff.
                record = dict(event)
            recorded.append({**record, 'recorded_at_ns': time.monotonic_ns()})
        speech = [event for event in recorded if event['type'] == 'speech']
        delegates = [i for i, event in enumerate(recorded) if event['type'] == 'delegate']
        kickoff = [i for i, event in enumerate(recorded) if event['type'] == 'speech' and event.get('beat') == 'kickoff']
        ordering_ok = case['category'] != 'long_work' or bool(delegates and kickoff and kickoff[0] < delegates[0])
        flags_ok = bool(speech and speech[-1].get('final') is True and all(
            event.get('final', False) is False for event in speech[:-1]))
        results.append({'case':case, 'chat':transcript['chat'], 'events':recorded,
                        'kickoff_before_delegation':ordering_ok, 'final_flags_valid':flags_ok})
    categories = {}
    expects_answer_failures = []
    for result in results:
        category = result['case']['category']
        counts = categories.setdefault(category, {'lines':0, 'passed':0, 'failing_lines':[]})
        for event in result['events']:
            if event['type'] != 'speech':
                continue
            counts['lines'] += 1
            if event['checker_reason'] is None:
                counts['passed'] += 1
            else:
                counts['failing_lines'].append({'id':result['case']['id'], 'text':event['text'], 'reason':event['checker_reason']})
            # Every expects_answer line must be a single direct question; a statement or a
            # multi-question line fails the whole run.
            if event.get('expects_answer') and event.get('expects_answer_reason') is not None:
                expects_answer_failures.append({'id':result['case']['id'], 'text':event['text'], 'reason':event['expects_answer_reason']})
    return {'categories':categories, 'transcripts':results,
            'expects_answer_failures':expects_answer_failures,
            'expects_answer_ok':not expects_answer_failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', required=True)
    parser.add_argument('--transcripts', required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text())
    server = None
    thread = None
    endpoint = args.endpoint
    if endpoint == 'fixture':
        ids = {case['conversation_id'] for case in cases}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'speaker':{'rules':{'replies':{'sentences_per_line':2,'words_per_line':40,'characters_per_line':300}}}}).encode())
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                result = {'outcome':'spoken'} if self.path == '/speak' and data.get('conversation_id') in ids else {'outcome':'refused','reason':'unknown_conversation'}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f'http://127.0.0.1:{server.server_port}'
    try:
        output = evaluate(cases, json.loads(Path(args.transcripts).read_text()), endpoint)
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join()
    Path(args.output).write_text(json.dumps(output, indent=2)+'\n')
    print(json.dumps({'categories':output['categories'], 'requests':len(output['transcripts'])}))


if __name__ == '__main__':
    main()
