import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
