import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

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

    def test_rejects_invalid_base_url(self):
        for base_url in ('', '   ', False, 123):
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(
                        LocalOAuthError, 'base_url must be a non-empty string'):
                    validate_local_oauth_config(_local_oauth(
                        'tokens.jsonl', base_url=base_url))


class LocalOAuthClientTests(unittest.TestCase):
    def test_rejects_unsafe_base_url_before_post(self):
        for base_url in (
                'http://customer.example.com', 'https://', 'customer.example.com',
                'https://user:secret@customer.example.com',
                'https://customer.example.com/path',
                'https://customer.example.com?query=1',
                'https://customer.example.com#fragment',
                'https://customer.example.com:invalid', 'https://[broken',
                'https://customer .example.com'):
            with self.subTest(base_url=base_url):
                session = mock.Mock()
                with self.assertRaisesRegex(LocalOAuthError, 'HTTPS origin'):
                    LocalOAuthClient(
                        _local_oauth('tokens.jsonl', base_url=base_url),
                        session=session)
                session.post.assert_not_called()

    def test_rejects_token_redirect_without_following_it(self):
        for status in (301, 302, 303, 307, 308):
            for location in ('http://other.example.com/token',
                             'https://other.example.com/token'):
                with self.subTest(status=status, location=location):
                    with tempfile.TemporaryDirectory() as directory:
                        response = requests.Response()
                        response.status_code = status
                        response.headers['Location'] = location
                        response._content = b'{}'
                        response.request = requests.Request(
                            'POST',
                            'https://login.salesforce.com/services/oauth2/token',
                        ).prepare()
                        response.url = response.request.url
                        adapter = mock.Mock(spec=requests.adapters.BaseAdapter)
                        adapter.send.return_value = response
                        with requests.Session() as session:
                            session.mount('https://', adapter)
                            session.mount('http://', adapter)
                            client = LocalOAuthClient(
                                _local_oauth(Path(directory) / 'tokens.jsonl'),
                                session=session)
                            with self.assertRaisesRegex(
                                    LocalOAuthError, 'redirects are not allowed'):
                                client.fetch_credentials('startup')
                        self.assertEqual(adapter.send.call_count, 1)
                        self.assertIs(adapter.send.call_args.kwargs['verify'], True)

    def test_rejects_unsafe_returned_api_url(self):
        for field in ('instance_url', 'service_url'):
            for url in ('http://api.example.com', 'https://',
                        'https://user:secret@api.example.com'):
                with self.subTest(field=field, url=url):
                    with tempfile.TemporaryDirectory() as directory:
                        session = mock.Mock()
                        session.post.return_value.json.return_value = {
                            'access_token': 'opaque.jwt-like.token',
                            field: url,
                        }
                        client = LocalOAuthClient(
                            _local_oauth(
                                Path(directory) / 'tokens.jsonl',
                                base_url='https://customer.example.com'),
                            session=session)
                        with self.assertRaisesRegex(LocalOAuthError, 'HTTPS origin'):
                            client.fetch_credentials('startup')

    def test_custom_endpoint_and_response_routing_precedence(self):
        cases = (
            ({}, 'https://customer.example.com'),
            ({'service_url': 'https://service.example.com'},
             'https://service.example.com'),
            ({'instance_url': 'https://instance.example.com',
              'service_url': 'https://service.example.com'},
             'https://instance.example.com'),
        )
        with tempfile.TemporaryDirectory() as directory:
            for routing, expected in cases:
                with self.subTest(routing=routing):
                    response = mock.Mock()
                    response.json.return_value = {
                        'access_token': 'access-token', **routing}
                    session = mock.Mock()
                    session.post.return_value = response
                    client = LocalOAuthClient(
                        _local_oauth(
                            Path(directory) / 'tokens.jsonl',
                            base_url='https://customer.example.com/',
                            is_sandbox=True),
                        session=session)

                    credentials = client.fetch_credentials('startup')

                    self.assertEqual(credentials['instance_url'], expected)
                    self.assertEqual(
                        session.post.call_args.args[0],
                        'https://customer.example.com/services/oauth2/token')

    def test_missing_routing_without_configured_base_url_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            session = mock.Mock()
            session.post.return_value.json.return_value = {
                'access_token': 'access-token'}
            client = LocalOAuthClient(
                _local_oauth(Path(directory) / 'tokens.jsonl'),
                session=session)

            self.assertEqual(
                client.token_url,
                'https://login.salesforce.com/services/oauth2/token')
            with self.assertRaisesRegex(
                    LocalOAuthError, 'missing instance_url'):
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
                timeout=LOCAL_OAUTH_REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False)
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
    def test_resolved_local_routing_is_applied_on_login_and_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            salesforce = Salesforce(
                default_start_date='2020-01-01T00:00:00Z',
                source_type='object',
                object_name='Account',
                auth_mode='local',
                local_oauth=_local_oauth(
                    Path(directory) / 'tokens.jsonl',
                    base_url='https://customer.example.com'))
            response = mock.Mock()
            response.json.side_effect = [
                {'access_token': 'access-1'},
                {'access_token': 'access-2',
                 'service_url': 'https://service.example.com'},
                {'access_token': 'access-3',
                 'instance_url': 'https://instance.example.com',
                 'service_url': 'https://service.example.com'},
            ]
            api_response = mock.Mock()
            api_response.headers = {}
            with mock.patch.object(
                    salesforce.session, 'post', return_value=response), \
                    mock.patch.object(
                        salesforce.session, 'get',
                        return_value=api_response) as get:
                for reason, expected in (
                        ('startup', 'https://customer.example.com'),
                        ('periodic', 'https://service.example.com'),
                        ('invalid_session', 'https://instance.example.com')):
                    salesforce._refresh_auth(reason)
                    self.assertEqual(salesforce.instance_url, expected)
                    url = salesforce.data_url.format(
                        salesforce.instance_url, 'sobjects')
                    salesforce._make_request('GET', url)
                    self.assertEqual(
                        get.call_args.args[0],
                        expected + '/services/data/v52.0/sobjects')

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
