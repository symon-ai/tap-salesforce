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


class LocalOAuthClientTests(unittest.TestCase):
    def test_uses_sandbox_endpoint_and_preserves_request_secrets_in_memory_only(self):
        with tempfile.TemporaryDirectory() as directory:
            response = mock.Mock(status_code=200)
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
            first_response = mock.Mock(status_code=200)
            first_response.json.return_value = {
                'access_token': 'access-token-1',
                'instance_url': 'https://example.my.salesforce.com',
                'issued_at': '1',
                'refresh_token': 'refresh-token-2',
            }
            first_response.raise_for_status.return_value = None
            second_response = mock.Mock(status_code=200)
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


    def test_api_base_precedence_and_custom_token_endpoint(self):
        base = 'https://oauth.example.com:8443/gateway'
        cases = [
            ({'instance_url': 'https://instance.example.com/org/',
              'service_url': 'https://service.example.com/org/'},
             'https://instance.example.com/org'),
            ({'instance_url': '  ', 'service_url': 'https://service.example.com/org/'},
             'https://service.example.com/org'),
            ({}, base),
        ]
        for routing_fields, expected_base in cases:
            with self.subTest(routing_fields=routing_fields), tempfile.TemporaryDirectory() as directory:
                session = mock.Mock()
                session.post.return_value = mock.Mock(status_code=200)
                session.post.return_value.json.return_value = {
                    'access_token': 'opaque-access-token', **routing_fields}
                client = LocalOAuthClient(_local_oauth(
                    Path(directory) / 'tokens.jsonl', is_sandbox=True,
                    base_url=f'{base}/'), session=session)

                self.assertEqual(client.fetch_credentials('startup')['instance_url'],
                                 expected_base)
                self.assertEqual(session.post.call_args.args[0],
                                 f'{base}/services/oauth2/token')
                self.assertFalse(session.post.call_args.kwargs['allow_redirects'])
                self.assertNotIn('verify', session.post.call_args.kwargs)

    def test_without_custom_url_service_still_works_but_missing_api_base_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            session = mock.Mock()
            session.post.return_value = mock.Mock(status_code=200)
            payload = {'access_token': 'opaque-access-token'}
            session.post.return_value.json.return_value = payload
            client = LocalOAuthClient(
                _local_oauth(Path(directory) / 'tokens.jsonl'), session=session)

            with self.assertRaisesRegex(LocalOAuthError, 'missing instance_url'):
                client.fetch_credentials('startup')
            payload['service_url'] = 'https://service.example.com'
            self.assertEqual(client.fetch_credentials('startup')['instance_url'],
                             'https://service.example.com')
            self.assertEqual(session.post.call_args.args[0],
                             'https://login.salesforce.com/services/oauth2/token')

    def test_rejects_unsafe_base_and_returned_api_urls(self):
        for url in ('http://api.example.com', 'https://127.0.0.1',
                    'https://api.example.com@evil.example.com',
                    'https://api.example.com?token=secret'):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                log_path = Path(directory) / 'tokens.jsonl'
                with self.assertRaisesRegex(LocalOAuthError, 'local_oauth.base_url'):
                    validate_local_oauth_config(_local_oauth(log_path, base_url=url))
                session = mock.Mock()
                session.post.return_value = mock.Mock(status_code=200)
                session.post.return_value.json.return_value = {
                    'access_token': 'opaque', 'instance_url': url,
                    'service_url': 'https://safe.example.com'}
                client = LocalOAuthClient(_local_oauth(log_path), session=session)
                with self.assertRaisesRegex(LocalOAuthError, 'response instance_url'):
                    client.fetch_credentials('startup')

    def test_rejects_redirect_before_reading_token_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            session = mock.Mock()
            response = session.post.return_value
            response.status_code = 307
            client = LocalOAuthClient(_local_oauth(
                Path(directory) / 'tokens.jsonl',
                base_url='https://oauth.example.com'), session=session)
            with self.assertRaisesRegex(LocalOAuthError, 'returned a redirect'):
                client.fetch_credentials('startup')
            session.post.assert_called_once()
            self.assertFalse(session.post.call_args.kwargs['allow_redirects'])
            response.json.assert_not_called()


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

    @mock.patch('backoff._sync.time.sleep')
    def test_rebases_authenticated_request_and_backoff_on_api_host_and_path_change(self, _):
        with tempfile.TemporaryDirectory() as directory:
            base = 'https://oauth.example.com/initial'
            refreshed_base = 'https://service.example.com/updated'
            salesforce = Salesforce(
                default_start_date='2020-01-01T00:00:00Z', source_type='object',
                object_name='Account', auth_mode='local', local_oauth=_local_oauth(
                    Path(directory) / 'tokens.jsonl', base_url=base))
            startup = mock.Mock(status_code=200)
            startup.json.return_value = {'access_token': 'old-opaque'}
            refreshed = mock.Mock(status_code=200)
            refreshed.json.return_value = {
                'access_token': 'new-opaque', 'service_url': refreshed_base}
            expired = requests.Response()
            expired.status_code = 401
            success = requests.Response()
            success.status_code = 200
            endpoint = '/services/data/v52.0/queryAll?cursor=123'
            with mock.patch.object(salesforce.session, 'post',
                                   side_effect=[startup, refreshed]) as token_post, \
                    mock.patch.object(salesforce.session, 'get', side_effect=[
                        expired, requests.exceptions.Timeout(), success]) as api_get:
                salesforce.login()
                result = salesforce._make_request(
                    'GET', base + endpoint,
                    headers={'Authorization': 'Bearer old-opaque',
                             'X-SFDC-Session': 'old-opaque'},
                    params={'q': 'SELECT Id FROM Account'})

            self.assertIs(result, success)
            self.assertEqual(salesforce.instance_url, refreshed_base)
            self.assertEqual([call.args[0] for call in token_post.call_args_list],
                             [f'{base}/services/oauth2/token'] * 2)
            self.assertEqual([call.args[0] for call in api_get.call_args_list],
                             [base + endpoint, refreshed_base + endpoint, refreshed_base + endpoint])
            for call in api_get.call_args_list[1:]:
                self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer new-opaque')
                self.assertEqual(call.kwargs['headers']['X-SFDC-Session'], 'new-opaque')
                self.assertEqual(call.kwargs['params'], {'q': 'SELECT Id FROM Account'})
                self.assertFalse(call.kwargs['allow_redirects'])

    def test_api_rejects_redirect_without_replaying_sfdc_session_header(self):
        with tempfile.TemporaryDirectory() as directory:
            base = 'https://api.example.com/gateway'
            salesforce = Salesforce(
                default_start_date='2020-01-01T00:00:00Z', source_type='object',
                object_name='Account', auth_mode='local', local_oauth=_local_oauth(
                    Path(directory) / 'tokens.jsonl', base_url=base))
            salesforce._set_session_credentials('opaque', base)
            response = mock.Mock(status_code=307)
            with mock.patch.object(salesforce.session, 'post', return_value=response) as post:
                with self.assertRaisesRegex(LocalOAuthError, 'API returned a redirect'):
                    salesforce._make_request(
                        'POST', base + '/services/async/52.0/job',
                        headers={'X-SFDC-Session': 'opaque'})
            post.assert_called_once()
            self.assertFalse(post.call_args.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()
