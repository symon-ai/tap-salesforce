import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from requests.exceptions import HTTPError

from tap_salesforce import main_impl, resolve_auth_mode, validate_config
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
    def test_endpoint_precedence_and_default_fallbacks(self):
        cases = [
            (None, False, {}, 'https://login.salesforce.com',
             'https://login.salesforce.com'),
            (None, True, {}, 'https://test.salesforce.com',
             'https://test.salesforce.com'),
            ('https://customer.example.com/', True, {},
             'https://customer.example.com', 'https://customer.example.com'),
            ('https://customer.example.com', False,
             {'service_url': 'https://api.example.com'},
             'https://customer.example.com', 'https://api.example.com'),
            ('https://customer.example.com', False,
             {'instance_url': 'https://instance.example.com',
              'service_url': 'https://api.example.com'},
             'https://customer.example.com', 'https://instance.example.com'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for base_url, sandbox, endpoints, token_base, api_base in cases:
                with self.subTest(base_url=base_url, endpoints=endpoints):
                    session = mock.Mock()
                    session.post.return_value.json.return_value = {
                        'access_token': 'opaque-access-token',
                        **endpoints,
                    }
                    client = LocalOAuthClient(
                        _local_oauth(
                            Path(directory) / 'tokens.jsonl',
                            is_sandbox=sandbox),
                        session=session,
                        base_url=base_url)

                    credentials = client.fetch_credentials('startup')

                    self.assertEqual(credentials['instance_url'], api_base)
                    self.assertEqual(
                        session.post.call_args.args[0],
                        token_base + '/services/oauth2/token')

    def test_missing_endpoint_on_refresh_keeps_last_api_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            session = mock.Mock()
            session.post.return_value.json.side_effect = [
                {'access_token': 'first', 'service_url': 'https://api.example.com'},
                {'access_token': 'second'},
            ]
            client = LocalOAuthClient(
                _local_oauth(Path(directory) / 'tokens.jsonl'),
                session=session,
                base_url='https://customer.example.com')

            client.fetch_credentials('startup')
            credentials = client.fetch_credentials('periodic')

            self.assertEqual(credentials['instance_url'], 'https://api.example.com')
            self.assertEqual(credentials['access_token'], 'second')
            self.assertEqual(
                [call.args[0] for call in session.post.call_args_list],
                ['https://customer.example.com/services/oauth2/token'] * 2)

    def test_endpoint_fallback_does_not_bypass_token_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            session = mock.Mock()
            session.post.return_value.json.return_value = {
                'service_url': 'https://api.example.com',
            }
            client = LocalOAuthClient(
                _local_oauth(Path(directory) / 'tokens.jsonl'),
                session=session,
                base_url='https://customer.example.com')

            with self.assertRaisesRegex(LocalOAuthError, 'missing access_token'):
                client.fetch_credentials('startup')

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


class LocalOAuthSalesforceTests(unittest.TestCase):
    def test_custom_base_url_does_not_override_broker_authentication(self):
        salesforce = Salesforce(
            default_start_date='2020-01-01T00:00:00Z',
            source_type='object',
            object_name='Account',
            auth_mode='broker',
            token_broker={
                'endpoint': 'https://broker.example/token',
                'task_auth_token': 'task-token',
            },
            local_oauth='ignored-invalid-local-config',
            base_url='https://customer.example.com')
        salesforce.session = mock.Mock()
        salesforce.session.post.return_value.json.return_value = {
            'accessToken': 'opaque-broker-token',
            'instanceUrl': 'https://broker-api.example.com',
            'tokenVersion': 'v1',
        }

        salesforce.login()

        self.assertIsNone(salesforce.local_oauth_client)
        self.assertEqual(salesforce.instance_url, 'https://broker-api.example.com')
        self.assertEqual(
            salesforce.session.post.call_args.args[0],
            'https://broker.example/token')
        self.assertEqual(
            salesforce.session.post.call_args.kwargs['headers']['Authorization'],
            'TaskAuth task-token')

    @mock.patch('tap_salesforce.do_discover')
    @mock.patch('tap_salesforce.singer_utils.parse_args')
    @mock.patch('tap_salesforce.salesforce.requests.Session')
    def test_main_passes_root_base_url_to_exchange_and_returned_endpoint_to_api(
            self, session_factory, parse_args, discover):
        with tempfile.TemporaryDirectory() as directory:
            config = _tap_config(_local_oauth(
                Path(directory) / 'tokens.jsonl', is_sandbox=True))
            config['base_url'] = 'https://customer.example.com/'
            parse_args.return_value = mock.Mock(config=config, discover=True)
            session = session_factory.return_value
            session.post.return_value.json.return_value = {
                'access_token': 'opaque-access-token',
                'service_url': 'https://api.example.com',
            }
            session.get.return_value.headers = {}
            discover.side_effect = lambda sf: sf.describe()

            with mock.patch('tap_salesforce.CONFIG', {}):
                main_impl()

            self.assertEqual(
                session.post.call_args.args[0],
                'https://customer.example.com/services/oauth2/token')
            self.assertEqual(
                session.get.call_args.args[0],
                'https://api.example.com/services/data/v52.0/sobjects/Account/describe')
            self.assertEqual(
                session.get.call_args.kwargs['headers']['Authorization'],
                'Bearer opaque-access-token')

    def test_refreshed_endpoint_and_token_reach_rest_and_bulk_requests(self):
        for reason in ('periodic', 'invalid_session'):
            for path in ('/services/data/v52.0/queryAll',
                         '/services/async/52.0/job'):
                with self.subTest(reason=reason, path=path):
                    with tempfile.TemporaryDirectory() as directory:
                        salesforce = Salesforce(
                            default_start_date='2020-01-01T00:00:00Z',
                            source_type='object',
                            object_name='Account',
                            auth_mode='local',
                            base_url='https://customer.example.com',
                            local_oauth=_local_oauth(
                                Path(directory) / 'tokens.jsonl'))
                        session = mock.Mock()
                        salesforce.session = session
                        salesforce.local_oauth_client.session = session
                        session.post.return_value.json.side_effect = [
                            {'access_token': 'first',
                             'instance_url': 'https://first.example.com'},
                            {'access_token': 'second',
                             'service_url': 'https://second.example.com'},
                        ]
                        success = mock.Mock(headers={})
                        if reason == 'periodic':
                            session.get.return_value = success
                        else:
                            invalid = mock.Mock(status_code=401)
                            invalid.raise_for_status.side_effect = HTTPError(
                                response=invalid)
                            session.get.side_effect = [invalid, success]
                        salesforce.login()
                        salesforce.refresh_check_after_seconds = (
                            0 if reason == 'periodic' else None)

                        response = salesforce._make_request(
                            'GET', 'https://first.example.com' + path,
                            headers=salesforce._get_standard_headers())

                        self.assertIs(response, success)
                        self.assertEqual(
                            session.get.call_args.args[0],
                            'https://second.example.com' + path)
                        self.assertEqual(
                            session.get.call_args.kwargs['headers']['Authorization'],
                            'Bearer second')
                        self.assertEqual(
                            session.get.call_count,
                            1 if reason == 'periodic' else 2)
                        self.assertEqual(
                            [call.args[0] for call in session.post.call_args_list],
                            ['https://customer.example.com/services/oauth2/token'] * 2)

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
