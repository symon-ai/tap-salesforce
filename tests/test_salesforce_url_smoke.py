import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import requests

import tap_salesforce


@contextmanager
def _http_endpoint(respond):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            calls.append((self.command, self.path, dict(self.headers), body))
            status, payload = respond(calls[-1])
            content = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:{}'.format(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


DESCRIPTION = {'fields': [{'name': 'Id', 'type': 'id'}]}
DESCRIBE_PATH = '/services/data/v52.0/sobjects/Account/describe'


class EntrypointURLSmokeTests(unittest.TestCase):
    def run_discovery(self, config, directory):
        config_path = Path(directory) / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        output = io.StringIO()
        verification = []
        original_send = requests.Session.send

        def send(session, request, **kwargs):
            verification.append((session.verify, kwargs.get('verify')))
            return original_send(session, request, **kwargs)

        with mock.patch.object(sys, 'argv', [
                'tap-salesforce', '--config', str(config_path), '--discover']), \
                mock.patch.object(tap_salesforce, 'CONFIG', {'start_date': None}), \
                mock.patch.object(requests.Session, 'send', new=send), \
                redirect_stdout(output):
            tap_salesforce.main_impl()

        self.assertEqual(json.loads(output.getvalue())['streams'][0]['stream'], 'Account')
        self.assertTrue(verification)
        self.assertTrue(all(pair == (True, True) for pair in verification))

    def config(self):
        return {
            'start_date': '2020-01-01T00:00:00Z',
            'api_type': 'REST',
            'select_fields_by_default': True,
            'source_type': 'object',
            'object_name': 'Account',
        }

    def test_entrypoint_custom_oauth_origin_and_refresh_api_origin(self):
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            refreshed_origin, refreshed_calls = stack.enter_context(
                _http_endpoint(lambda _call: (200, DESCRIPTION)))
            initial_origin, initial_calls = stack.enter_context(_http_endpoint(
                lambda _call: (401, [{'errorCode': 'INVALID_SESSION_ID',
                                     'message': 'Synthetic expired session'}])))
            responses = iter([
                {'access_token': 'opaque-startup-token',
                 'instance_url': initial_origin, 'service_url': refreshed_origin,
                 'refresh_token': 'rotated-refresh-token', 'issued_at': '1'},
                {'access_token': 'opaque-refreshed-token',
                 'service_url': refreshed_origin, 'issued_at': '2'},
            ])
            oauth_origin, oauth_calls = stack.enter_context(_http_endpoint(
                lambda _call: (200, next(responses))))
            config = self.config()
            config.update({
                'base_url': oauth_origin + '/',
                'auth_mode': 'local',
                'local_oauth': {
                    'client_id': 'synthetic-client',
                    'client_secret': 'synthetic-secret',
                    'refresh_token': 'initial-refresh-token',
                    'is_sandbox': True,
                    'refresh_token_log_path': str(Path(directory) / 'tokens.jsonl'),
                },
            })

            self.run_discovery(config, directory)

            self.assertEqual(len(oauth_calls), 2)
            self.assertEqual(
                [(call[0], call[1]) for call in oauth_calls],
                [('POST', '/services/oauth2/token')] * 2)
            self.assertEqual(
                [parse_qs(call[3].decode())['refresh_token'][0] for call in oauth_calls],
                ['initial-refresh-token', 'rotated-refresh-token'])
            self.assertEqual(len(initial_calls), 1)
            self.assertEqual(len(refreshed_calls), 1)
            self.assertEqual(initial_calls[0][1], DESCRIBE_PATH)
            self.assertEqual(refreshed_calls[0][1], DESCRIBE_PATH)
            self.assertEqual(initial_calls[0][2]['Authorization'],
                             'Bearer opaque-startup-token')
            self.assertEqual(refreshed_calls[0][2]['Authorization'],
                             'Bearer opaque-refreshed-token')
            entries = (Path(directory) / 'tokens.jsonl').read_text().splitlines()
            self.assertEqual(len(entries), 1)
            self.assertEqual(json.loads(entries[0])['refresh_token'],
                             'rotated-refresh-token')

    def test_entrypoint_broker_precedence_skips_local_exchange(self):
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            api_origin, api_calls = stack.enter_context(
                _http_endpoint(lambda _call: (200, DESCRIPTION)))
            oauth_origin, oauth_calls = stack.enter_context(
                _http_endpoint(lambda _call: (500, {})))
            broker_origin, broker_calls = stack.enter_context(_http_endpoint(
                lambda _call: (200, {
                    'accessToken': 'opaque-broker-token',
                    'instanceUrl': api_origin,
                    'tokenVersion': '1',
                })))
            config = self.config()
            config.update({
                'base_url': oauth_origin,
                'auth_mode': 'local',
                'local_oauth': 'ignored-invalid-local-config',
                'token_broker': {
                    'endpoint': broker_origin + '/token',
                    'connection_id': 'synthetic-connection',
                    'task_auth_token': 'synthetic-task-token',
                },
            })

            self.run_discovery(config, directory)

            self.assertEqual(oauth_calls, [])
            self.assertEqual(len(broker_calls), 1)
            self.assertEqual(json.loads(broker_calls[0][3])['reason'], 'startup')
            self.assertEqual(len(api_calls), 1)
            self.assertEqual(api_calls[0][1], DESCRIBE_PATH)
            self.assertEqual(api_calls[0][2]['Authorization'],
                             'Bearer opaque-broker-token')


if __name__ == '__main__':
    unittest.main()
