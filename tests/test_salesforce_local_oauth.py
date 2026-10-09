import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
from urllib.parse import parse_qs

import requests

import tap_salesforce

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


class LocalOAuthRoutingTests(unittest.TestCase):
    def _run_discovery(self, config, token_payloads, api_payloads=None):
        token_responses = iter(token_payloads)
        api_responses = iter(api_payloads or [
            (200, {'name': 'Account'}), (200, {'name': 'Account'})])
        requests_seen = []
        routes_seen = []

        def send(adapter, request, **kwargs):
            requests_seen.append((request, kwargs))
            response = requests.Response()
            response.request = request
            response.url = request.url
            if request.method == 'POST':
                response.status_code = 200
                payload = next(token_responses)
            else:
                response.status_code, payload = next(api_responses)
            response._content = json.dumps(payload).encode('utf-8')
            response.headers['Content-Type'] = 'application/json'
            return response

        def discover(salesforce):
            self.assertEqual(salesforce.describe(), {'name': 'Account'})
            routes_seen.append(salesforce.instance_url)
            # Exercise the real periodic refresh before sending the next read.
            salesforce._last_broker_check_at = float('-inf')
            self.assertEqual(salesforce.describe(), {'name': 'Account'})
            routes_seen.append(salesforce.instance_url)
            if api_payloads is not None:
                # The next read receives INVALID_SESSION_ID and must refresh
                # and retry against the provider's retained or updated route.
                self.assertEqual(salesforce.describe(), {'name': 'Account'})
                routes_seen.append(salesforce.instance_url)

        args = SimpleNamespace(config=config, discover=True)
        with mock.patch.dict(tap_salesforce.CONFIG, {}, clear=True), \
                mock.patch('tap_salesforce.singer_utils.parse_args',
                           return_value=args), \
                mock.patch('tap_salesforce.do_discover', side_effect=discover), \
                mock.patch.object(requests.adapters.HTTPAdapter, 'send', send):
            tap_salesforce.main_impl()

        for request, kwargs in requests_seen:
            self.assertTrue(kwargs['verify'])
            if request.method == 'GET':
                self.assertTrue(request.headers['Authorization'].startswith(
                    'Bearer opaque-token'))
        return requests_seen, routes_seen

    def test_entrypoint_routes_custom_and_default_urls_and_retains_on_refresh(self):
        cases = [
            ('custom-service', 'https://oauth.customer.example/', False,
             {'service_url': 'https://api.customer.example'},
             'https://api.customer.example'),
            ('instance-first', 'https://oauth.customer.example', False,
             {'instance_url': 'https://instance.example',
              'service_url': 'https://service.example'},
             'https://instance.example'),
            ('empty-instance', 'https://oauth.customer.example', False,
             {'instance_url': ' ', 'service_url': 'https://service.example'},
             'https://service.example'),
            ('custom-fallback', 'https://oauth.customer.example', False,
             {}, 'https://oauth.customer.example'),
            ('production-default', None, False, {},
             'https://login.salesforce.com'),
            ('sandbox-default', None, True, {},
             'https://test.salesforce.com'),
            ('custom-over-sandbox', 'https://oauth.customer.example', True,
             {'instance_url': '', 'service_url': ''},
             'https://oauth.customer.example'),
        ]
        for name, base_url, sandbox, aliases, expected_route in cases:
            with self.subTest(name=name), \
                    tempfile.TemporaryDirectory() as directory:
                config = _tap_config(_local_oauth(
                    Path(directory) / 'tokens.jsonl', is_sandbox=sandbox))
                if base_url is not None:
                    config['base_url'] = base_url
                initial = dict(aliases, access_token='opaque-token-1')
                seen, routes = self._run_discovery(config, [
                    initial, {'access_token': 'opaque-token-2'}])

                oauth_base = (base_url or (
                    'https://test.salesforce.com' if sandbox
                    else 'https://login.salesforce.com')).rstrip('/')
                self.assertEqual(
                    [request.url for request, _ in seen],
                    [oauth_base + '/services/oauth2/token',
                     expected_route + '/services/data/v52.0/sobjects/Account/describe',
                     oauth_base + '/services/oauth2/token',
                     expected_route + '/services/data/v52.0/sobjects/Account/describe'])
                self.assertEqual(routes, [expected_route, expected_route])
                self.assertEqual(
                    seen[-1][0].headers['Authorization'], 'Bearer opaque-token-2')
                self.assertEqual(
                    parse_qs(seen[0][0].body)['grant_type'], ['refresh_token'])

    def test_periodic_route_change_and_invalid_session_refresh_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _tap_config(_local_oauth(Path(directory) / 'tokens.jsonl'))
            config['base_url'] = 'https://oauth.customer.example'
            seen, routes = self._run_discovery(config, [
                {'access_token': 'opaque-token-1',
                 'service_url': 'https://first-api.example'},
                {'access_token': 'opaque-token-2',
                 'service_url': 'https://second-api.example'},
                {'access_token': 'opaque-token-3'},
            ], api_payloads=[
                (200, {'name': 'Account'}),
                (200, {'name': 'Account'}),
                (401, [{'errorCode': 'INVALID_SESSION_ID'}]),
                (200, {'name': 'Account'}),
            ])
            self.assertEqual(routes, [
                'https://first-api.example', 'https://second-api.example',
                'https://second-api.example'])
            self.assertEqual(
                [request.url for request, _ in seen if request.method == 'POST'],
                ['https://oauth.customer.example/services/oauth2/token'] * 3)
            self.assertEqual(
                [request.url for request, _ in seen if request.method == 'GET'],
                ['https://first-api.example/services/data/v52.0/sobjects/Account/describe']
                + ['https://second-api.example/services/data/v52.0/sobjects/Account/describe'] * 3)
            self.assertEqual(
                seen[-1][0].headers['Authorization'], 'Bearer opaque-token-3')

    def test_broker_route_remains_authoritative_with_custom_base_and_local_mode(self):
        config = _tap_config('ignored-invalid-local-config')
        config['base_url'] = 'https://ignored-oauth.example'
        config['token_broker'] = {
            'endpoint': 'https://broker.example/token',
            'connection_id': 'connection-id',
            'task_auth_token': 'task-token',
        }
        seen, routes = self._run_discovery(config, [
            {'accessToken': 'opaque-token-1', 'tokenVersion': '1',
             'instanceUrl': 'https://broker-api.example'},
            {'accessToken': 'opaque-token-2', 'tokenVersion': '2',
             'instanceUrl': 'https://refreshed-broker-api.example'},
        ])
        self.assertEqual(routes, [
            'https://broker-api.example', 'https://refreshed-broker-api.example'])
        self.assertEqual(
            [request.url for request, _ in seen],
            ['https://broker.example/token',
             'https://broker-api.example/services/data/v52.0/sobjects/Account/describe',
             'https://broker.example/token',
             'https://refreshed-broker-api.example/services/data/v52.0/sobjects/Account/describe'])
        for request, _ in seen:
            if request.method == 'POST':
                self.assertEqual(request.headers['Authorization'], 'TaskAuth task-token')
        self.assertEqual(
            json.loads(seen[2][0].body)['knownTokenVersion'], '1')


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
