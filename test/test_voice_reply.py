import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tools.voice_line import check_line
from tools.voice_reply import main, submit_line


class LineTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertIsNone(check_line(' '.join(['go'] * 40)))
        self.assertEqual(check_line(' '.join(['go'] * 41)), 'words')
        self.assertIsNone(check_line('x' * 300))
        self.assertEqual(check_line('a' * 301), 'characters')
        self.assertIsNone(check_line('It is done. Details are in chat.'))
        self.assertEqual(check_line('It is done. Read chat. Thank you.'), 'sentences')

    def test_every_structural_rule(self):
        for line in ['See https://example.com.', 'See www.example.org.', 'Read example.com.',
                     'Mail a@b.org.', 'Use `code`.', '*Ready*', 'One\nTwo', '- Ready',
                     'Read /tmp/file.', 'Read C:\\temp.', 'Use snake_case.', 'Use camelCase.',
                     'Use ab12cdef.', 'Read workstation:seat.', 'Use v2.', 'Run $(echo ready).']:
            with self.subTest(line=line):
                self.assertIsNotNone(check_line(line))
        self.assertEqual(check_line('Is it ready? Can I start?'), 'questions')
        self.assertEqual(check_line('It is 1, 2, 3 and 4.'), 'numbers')
        self.assertEqual(check_line('It is about 1.234 seconds.'), 'unrounded_number')
        self.assertIsNone(check_line('It is about 1.5 seconds.'))


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.result = {'outcome': 'spoken'}
        self.delay = 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                time.sleep(owner.delay)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(owner.result).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_all_outcomes_and_literal_text(self):
        text = "It's ready; wait & stay calm (please)."
        for result in [{'outcome':'spoken'}, {'outcome':'suppressed','reason':'silent'}, {'outcome':'refused','reason':'unknown_conversation'}]:
            self.result = result
            self.assertEqual(submit_line('id', 'reply', text, 'chat', True, self.url), result)
        self.assertEqual(self.seen[0], ('/speak', {'conversation_id':'id','kind':'reply','text':text,'action':'chat','final':True}))

    def test_refusal_before_network(self):
        for line in ['a' * 301, ' '.join(['go'] * 41), 'One. Two. Three.']:
            self.assertEqual(submit_line('id', 'reply', line, endpoint=self.url)['outcome'], 'refused')
        self.assertEqual(submit_line('', 'reply', 'Ready.', endpoint=self.url)['reason'], 'missing_conversation_id')
        self.assertEqual(submit_line('id', 'reply', 'Ready.', endpoint='http://remote.invalid')['reason'], 'nonlocal_endpoint')
        self.assertEqual(self.seen, [])

    def test_unknown_typed_header_is_data_for_the_service_to_refuse(self):
        self.result = {'outcome':'refused','reason':'unknown_conversation'}
        self.assertEqual(submit_line('hand-written-id', 'reply', 'Ready.', endpoint=self.url), self.result)

    def test_bad_response_and_unavailable_service_are_nonfatal(self):
        self.result = {'ok':True}
        self.assertEqual(submit_line('id', 'reply', 'Ready.', endpoint=self.url)['reason'], 'invalid_response')
        self.assertEqual(submit_line('id', 'reply', 'Ready.', endpoint='http://127.0.0.1:1')['reason'], 'speaker_unavailable')

    def test_synchronous_synthesis_receipt_can_take_longer_than_two_seconds(self):
        self.delay = 2.1
        self.assertEqual(submit_line('id', 'reply', 'Ready.', endpoint=self.url), {'outcome':'spoken'})

    def test_cli_prints_outcome_and_returns_success_on_refusal(self):
        with patch('builtins.print') as output:
            self.assertEqual(main(['--conversation-id','id','--text','a'*301]), 0)
            self.assertEqual(json.loads(output.call_args.args[0])['outcome'], 'refused')


if __name__ == '__main__':
    unittest.main()
