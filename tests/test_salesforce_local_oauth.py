import json
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest import mock
from urllib.parse import parse_qs

from requests import HTTPError

from tap_salesforce import resolve_auth_mode, validate_config
from tap_salesforce.salesforce import Salesforce
from tap_salesforce.salesforce.local_oauth import (
    LOCAL_OAUTH_REQUEST_TIMEOUT_SECONDS,
    LocalOAuthClient,
    LocalOAuthError,
    validate_local_oauth_config,
)


def _local_oauth(log_path, **overrides):
    config = {
        'client_id': 'client-id',
        'client_secret': 'client-secret',
        'refresh_token': 'refresh-token-1',
        'refresh_token_log_path': str(log_path),
    }
    config.update(overrides)
    return config


def _tap_config(local_oauth):
    return {
        'start_date': '2020-01-01T00:00:00Z',
        'api_type': 'REST',
        'select_fields_by_default': True,
        'source_type': 'object',
        'object_name': 'Account',
        'auth_mode': 'local',
        'local_oauth': local_oauth,
    }


class LocalOAuthConfigTests(unittest.TestCase):
    def test_accepts_explicit_local_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _tap_config(_local_oauth(
                Path(directory) / 'tokens.jsonl'))

            validate_config(config)

            self.assertEqual(resolve_auth_mode(config), 'local')

    def test_local_oauth_block_does_not_enable_mode_implicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _tap_config(_local_oauth(
                Path(directory) / 'tokens.jsonl'))
            del config['auth_mode']

            with self.assertRaisesRegex(
                    Exception, "token_broker is required unless auth_mode is 'local'"):
                validate_config(config)

    def test_token_broker_takes_precedence_over_local_mode(self):
        config = _tap_config('ignored-invalid-local-config')
        config['token_broker'] = {
            'endpoint': 'https://broker.example/token',
            'connection_id': 'connection-id',
            'task_auth_token': 'task-token',
        }

        validate_config(config)

        self.assertEqual(resolve_auth_mode(config), 'broker')

    def test_rejects_non_boolean_sandbox_flag(self):
        with self.assertRaisesRegex(
                LocalOAuthError, 'is_sandbox must be a boolean'):
            validate_local_oauth_config(_local_oauth(
                'tokens.jsonl',
                is_sandbox='true'))


class LocalOAuthClientTests(unittest.TestCase):
    def test_uses_sandbox_endpoint_and_preserves_request_secrets_in_memory_only(self):
        with tempfile.TemporaryDirectory() as directory:
            response = mock.Mock()
            response.json.return_value = {
                'access_token': 'access-token',
                'instance_url': 'https://example.my.salesforce.com',
                'issued_at': '12345',
            }
            response.raise_for_status.return_value = None
            session = mock.Mock()
            session.post.return_value = response
            log_path = Path(directory) / 'tokens.jsonl'
            client = LocalOAuthClient(
                _local_oauth(log_path, is_sandbox=True),
                session=session)

            credentials = client.fetch_credentials('startup')

            self.assertEqual(credentials['access_token'], 'access-token')
            session.post.assert_called_once_with(
                'https://test.salesforce.com/services/oauth2/token',
                data={
                    'grant_type': 'refresh_token',
                    'client_id': 'client-id',
                    'client_secret': 'client-secret',
                    'refresh_token': 'refresh-token-1',
                },
                timeout=LOCAL_OAUTH_REQUEST_TIMEOUT_SECONDS)
            self.assertEqual(log_path.read_text(encoding='utf-8'), '')

    @mock.patch('tap_salesforce.salesforce.local_oauth.LOGGER')
    def test_appends_each_rotated_token_with_timestamp_and_uses_latest_token(
            self, logger):
        with tempfile.TemporaryDirectory() as directory:
            first_response = mock.Mock()
            first_response.json.return_value = {
                'access_token': 'access-token-1',
                'instance_url': 'https://example.my.salesforce.com',
                'issued_at': '1',
                'refresh_token': 'refresh-token-2',
            }
            first_response.raise_for_status.return_value = None
            second_response = mock.Mock()
            second_response.json.return_value = {
                'access_token': 'access-token-2',
                'instance_url': 'https://example.my.salesforce.com',
                'issued_at': '2',
                'refresh_token': 'refresh-token-3',
            }
            second_response.raise_for_status.return_value = None
            session = mock.Mock()
            session.post.side_effect = [first_response, second_response]
            log_path = Path(directory) / 'tokens.jsonl'
            client = LocalOAuthClient(_local_oauth(log_path), session=session)

            client.fetch_credentials('startup')
            client.fetch_credentials('periodic')

            entries = [
                json.loads(line)
                for line in log_path.read_text(encoding='utf-8').splitlines()
            ]
            self.assertEqual(
                [entry['refresh_token'] for entry in entries],
                ['refresh-token-2', 'refresh-token-3'])
            self.assertTrue(all(entry['timestamp'].endswith('Z')
                                for entry in entries))
            self.assertEqual(
                session.post.call_args_list[1].kwargs['data']['refresh_token'],
                'refresh-token-2')
            logged = str(logger.mock_calls)
            self.assertNotIn('refresh-token-1', logged)
            self.assertNotIn('refresh-token-2', logged)
            self.assertNotIn('refresh-token-3', logged)
            self.assertIn('new_refresh_token_returned=%s', logged)
            self.assertIn('refresh_token_logged=%s', logged)
            self.assertIn(str(log_path), logged)


class LocalOAuthEndpointTests(unittest.TestCase):
    def test_token_and_api_endpoint_precedence(self):
        cases = [
            ({}, None, {}, 'https://login.salesforce.com',
             'https://login.salesforce.com'),
            ({'is_sandbox': True}, None, {}, 'https://test.salesforce.com',
             'https://test.salesforce.com'),
            ({}, 'https://customer.example///', {}, 'https://customer.example',
             'https://customer.example'),
            ({'base_url': 'https://local.example/'},
             'https://top.example', {}, 'https://local.example',
             'https://local.example'),
            ({'is_sandbox': True}, 'https://customer.example/',
             {'service_url': 'https://service.example'},
             'https://customer.example', 'https://service.example'),
            ({}, 'https://customer.example',
             {'instance_url': 'https://instance.example',
              'service_url': 'https://service.example'},
             'https://customer.example', 'https://instance.example'),
            ({}, None,
             {'instance_url': ' \t', 'service_url': 'https://service.example'},
             'https://login.salesforce.com', 'https://service.example'),
            ({}, None, {'instance_url': 42, 'service_url': ['invalid']},
             'https://login.salesforce.com', 'https://login.salesforce.com'),
            ({}, None, {'instance_url': None, 'service_url': ' \n'},
             'https://login.salesforce.com', 'https://login.salesforce.com'),
        ]
        for local_settings, base_url, payload, token_base, api_base in cases:
            with self.subTest(local_settings=local_settings, payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    local_config = _local_oauth(
                        Path(directory) / 'tokens.jsonl', **local_settings)
                    original_config = dict(local_config)
                    with mock.patch(
                            'tap_salesforce.salesforce.requests.Session') as session_factory:
                        session = session_factory.return_value
                        session.post.return_value.json.return_value = dict(
                            access_token='opaque-not-a-jwt', **payload)
                        session.get.return_value.json.return_value = {'fields': []}
                        session.get.return_value.headers = {}
                        sf = Salesforce(
                            default_start_date='2020-01-01T00:00:00Z',
                            source_type='object',
                            object_name='Account',
                            auth_mode='local',
                            local_oauth=local_config,
                            base_url=base_url)
                        sf.login()
                        self.assertEqual(sf.describe(), {'fields': []})
                        self.assertEqual(
                            session.post.call_args.args[0],
                            token_base + '/services/oauth2/token')
                        self.assertEqual(
                            session.get.call_args.args[0],
                            api_base + '/services/data/v52.0/sobjects/Account/describe')
                        self.assertEqual(
                            session.get.call_args.kwargs['headers']['Authorization'],
                            'Bearer opaque-not-a-jwt')
                        self.assertEqual(local_config, original_config)

    def test_rejects_unsafe_or_invalid_configured_base_before_token_request(self):
        invalid_urls = [
            '', ' \t', False, 42, {}, [], 'http://customer.example',
            'customer.example', 'https://', 'https://user@customer.example',
            'https://customer.example?query=value',
            'https://customer.example#fragment', 'https://[invalid',
        ]
        for value in invalid_urls:
            for local_override in (False, True):
                with self.subTest(value=value, local_override=local_override):
                    with tempfile.TemporaryDirectory() as directory:
                        config = _local_oauth(Path(directory) / 'tokens.jsonl')
                        if local_override:
                            config['base_url'] = value
                        session = mock.Mock()
                        with self.assertRaisesRegex(LocalOAuthError, 'base_url'):
                            LocalOAuthClient(
                                config, session=session,
                                base_url=None if local_override else value)
                        session.post.assert_not_called()

    def test_explicit_null_local_base_does_not_fall_back(self):
        session = mock.Mock()
        with self.assertRaisesRegex(LocalOAuthError, 'base_url'):
            LocalOAuthClient(
                _local_oauth('unused-tokens.jsonl', base_url=None),
                session=session, base_url='https://top.example')
        session.post.assert_not_called()

    def test_refresh_rebases_api_path_and_preserves_query(self):
        for reason in ('periodic', 'invalid_session'):
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory() as directory:
                    with mock.patch(
                            'tap_salesforce.salesforce.requests.Session') as factory:
                        session = factory.return_value
                        session.post.return_value.json.side_effect = [
                            {'access_token': 'old-token',
                             'instance_url': 'https://gateway.example/old'},
                            {'access_token': 'new-token',
                             'instance_url': 'https://gateway.example/new'},
                        ]
                        success = mock.Mock(headers={})
                        success.json.return_value = {'records': []}
                        if reason == 'invalid_session':
                            invalid = mock.Mock(status_code=401)
                            invalid.raise_for_status.side_effect = HTTPError(
                                response=invalid)
                            session.get.side_effect = [invalid, success]
                        else:
                            session.get.return_value = success
                        sf = Salesforce(
                            default_start_date='2020-01-01T00:00:00Z',
                            source_type='object', object_name='Account',
                            auth_mode='local',
                            local_oauth=_local_oauth(Path(directory) / 'tokens.jsonl'))
                        sf.login()
                        if reason == 'periodic':
                            sf._last_broker_check_at = 0
                        suffix = '/services/data/v52.0/query?q=SELECT%20Id'
                        response = sf._make_request(
                            'GET', sf.instance_url + suffix,
                            headers={'Authorization': 'Bearer old-token'})
                        self.assertEqual(response.json(), {'records': []})
                        self.assertEqual(
                            session.get.call_args.args[0],
                            'https://gateway.example/new' + suffix)
                        self.assertEqual(
                            session.get.call_args.kwargs['headers']['Authorization'],
                            'Bearer new-token')
                        self.assertEqual(session.post.call_count, 2)

    def test_broker_keeps_returned_instance_url(self):
        with mock.patch(
                'tap_salesforce.salesforce.fetch_broker_credentials',
                return_value={
                    'access_token': 'opaque-token',
                    'instance_url': 'https://broker-instance.example',
                    'token_version': '1',
                }):
            sf = Salesforce(
                default_start_date='2020-01-01T00:00:00Z',
                source_type='object',
                object_name='Account',
                token_broker={'endpoint': 'https://broker.example',
                              'task_auth_token': 'task-token'},
                base_url='https://ignored.example')
            sf.login()
            self.assertEqual(sf.instance_url, 'https://broker-instance.example')
            self.assertIsNone(sf.local_oauth_client)


class LocalOAuthHttpSmokeTests(unittest.TestCase):
    def test_local_https_exchange_and_api_base_paths_change_on_refresh(self):
        exchanges = []
        requests = []
        token_payloads = []
        reject_next_request = [False]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, payload):
                body = json.dumps(payload).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                exchanges.append((
                    self.server.server_port, self.path,
                    parse_qs(body.decode('utf-8'))))
                self.respond(200, token_payloads.pop(0))

            def do_GET(self):
                requests.append((
                    self.server.server_port, self.path,
                    self.headers.get('Authorization')))
                if reject_next_request[0]:
                    reject_next_request[0] = False
                    self.respond(401, [{'errorCode': 'INVALID_SESSION_ID'}])
                else:
                    self.respond(200, {'fields': []})

        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            certificate = Path(directory) / 'certificate.pem'
            private_key = Path(directory) / 'private-key.pem'
            subprocess.run([
                'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(private_key), '-out', str(certificate),
                '-days', '1', '-subj', '/CN=127.0.0.1',
                '-addext', 'subjectAltName=IP:127.0.0.1',
                '-addext', 'basicConstraints=critical,CA:TRUE',
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.load_cert_chain(certificate, private_key)
            auth_server = stack.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Handler))
            api_server = stack.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Handler))
            for server in (auth_server, api_server):
                server.socket = tls_context.wrap_socket(
                    server.socket, server_side=True)
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                stack.callback(thread.join)
                stack.callback(server.shutdown)
            auth_base = 'https://127.0.0.1:{}'.format(auth_server.server_port)
            api_base = 'https://127.0.0.1:{}'.format(api_server.server_port)
            token_payloads.extend([
                {'access_token': 'opaque-1', 'instance_url': api_base + '/old',
                 'service_url': auth_base, 'refresh_token': 'refresh-token-2'},
                {'access_token': 'opaque-2', 'service_url': auth_base + '/new'},
                {'access_token': 'opaque-3'},
                {'access_token': 'opaque-4', 'instance_url': api_base + '/latest'},
            ])
            sf = Salesforce(
                default_start_date='2020-01-01T00:00:00Z',
                source_type='object',
                object_name='Account',
                auth_mode='local',
                base_url=auth_base + '/',
                local_oauth=_local_oauth(Path(directory) / 'tokens.jsonl'))
            stack.callback(sf.session.close)
            sf.session.verify = str(certificate)
            sf.login()
            api_path = '/services/data/v52.0/sobjects/Account/describe?detail=true'

            def request_api():
                return sf._make_request(
                    'GET', sf.instance_url + api_path,
                    headers=sf._get_standard_headers()).json()

            self.assertEqual(request_api(), {'fields': []})
            reject_next_request[0] = True
            self.assertEqual(request_api(), {'fields': []})
            sf._last_broker_check_at = 0
            self.assertEqual(request_api(), {'fields': []})
            sf._last_broker_check_at = 0
            self.assertEqual(request_api(), {'fields': []})

            self.assertEqual(
                [(port, path) for port, path, _ in exchanges],
                [(auth_server.server_port, '/services/oauth2/token')] * 4)
            self.assertEqual(
                [body['refresh_token'] for _, _, body in exchanges],
                [['refresh-token-1'], ['refresh-token-2'],
                 ['refresh-token-2'], ['refresh-token-2']])
            self.assertEqual(requests, [
                (api_server.server_port, '/old' + api_path, 'Bearer opaque-1'),
                (api_server.server_port, '/old' + api_path, 'Bearer opaque-1'),
                (auth_server.server_port, '/new' + api_path, 'Bearer opaque-2'),
                (auth_server.server_port, api_path, 'Bearer opaque-3'),
                (api_server.server_port, '/latest' + api_path, 'Bearer opaque-4'),
            ])
            self.assertEqual(sf.instance_url, api_base + '/latest')
            self.assertEqual(token_payloads, [])


class LocalOAuthSalesforceTests(unittest.TestCase):
    @mock.patch.object(Salesforce, '_login_local')
    def test_existing_refresh_reasons_dispatch_to_local_oauth(self, login_local):
        with tempfile.TemporaryDirectory() as directory:
            salesforce = Salesforce(
                default_start_date='2020-01-01T00:00:00Z',
                source_type='object',
                object_name='Account',
                auth_mode='local',
                local_oauth=_local_oauth(
                    Path(directory) / 'tokens.jsonl'))

            salesforce.login()
            salesforce._refresh_auth('periodic')
            salesforce._refresh_auth('invalid_session')

            self.assertEqual(
                login_local.call_args_list,
                [
                    mock.call('startup'),
                    mock.call('periodic'),
                    mock.call('invalid_session'),
                ])


if __name__ == '__main__':
    unittest.main()
