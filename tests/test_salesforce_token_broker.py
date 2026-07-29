import json
import threading
import unittest
from unittest import mock

import requests
from requests.exceptions import HTTPError

from tap_salesforce import validate_config
from tap_salesforce.salesforce import Salesforce, REFRESH_TOKEN_EXPIRATION_PERIOD
from tap_salesforce.salesforce.token_broker import (
    BROKER_MAX_ATTEMPTS,
    BROKER_REQUEST_TIMEOUT_SECONDS,
    TokenBrokerError,
    build_broker_request,
    fetch_broker_credentials,
    parse_broker_response,
)


def _base_salesforce_kwargs(**overrides):
    config = {
        'refresh_token': 'legacy-refresh',
        'sf_client_id': 'client-id',
        'sf_client_secret': 'client-secret',
        'default_start_date': '2020-01-01T00:00:00Z',
        'source_type': 'object',
        'object_name': 'Account',
    }
    config.update(overrides)
    return config


class TokenBrokerRequestTests(unittest.TestCase):
    def test_build_broker_request_includes_reason_and_known_version(self):
        request = build_broker_request(
            'https://broker.example/token',
            'invalid_session',
            known_token_version='v1')

        self.assertEqual(request['url'], 'https://broker.example/token')
        self.assertEqual(request['headers']['Content-Type'], 'application/json')
        self.assertEqual(
            json.loads(request['body']),
            {
                'reason': 'invalid_session',
                'knownTokenVersion': 'v1',
            })

    def test_fetch_broker_credentials_sets_task_auth_header(self):
        response = mock.Mock()
        response.json.return_value = {
            'accessToken': 'sf-access',
            'instanceUrl': 'https://example.my.salesforce.com',
            'tokenVersion': 'v2',
            'refreshCheckAfterSeconds': 1200,
        }
        response.raise_for_status = mock.Mock()

        session = mock.Mock()
        session.post.return_value = response

        credentials = fetch_broker_credentials(
            endpoint='https://broker.example/token',
            reason='startup',
            task_auth_token='task-token-abc',
            session=session)

        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        self.assertEqual(
            kwargs['headers']['Authorization'],
            'TaskAuth task-token-abc')
        self.assertEqual(
            json.loads(kwargs['data']),
            {
                'reason': 'startup',
            })
        self.assertEqual(credentials['access_token'], 'sf-access')
        self.assertEqual(credentials['instance_url'],
                         'https://example.my.salesforce.com')
        self.assertEqual(credentials['token_version'], 'v2')
        self.assertEqual(credentials['refresh_check_after_seconds'], 1200)
        self.assertEqual(kwargs['timeout'], BROKER_REQUEST_TIMEOUT_SECONDS)

    @mock.patch('tap_salesforce.salesforce.token_broker.time.sleep')
    def test_fetch_broker_credentials_does_not_leak_token_on_failure(
            self, mock_sleep):
        session = mock.Mock()
        session.post.side_effect = requests.exceptions.Timeout('timed out')

        with self.assertRaises(TokenBrokerError) as ctx:
            fetch_broker_credentials(
                endpoint='https://broker.example/token',
                reason='startup',
                task_auth_token='secret-task-token',
                session=session)

        self.assertNotIn('secret-task-token', str(ctx.exception))
        self.assertEqual(session.post.call_count, BROKER_MAX_ATTEMPTS)
        self.assertEqual(mock_sleep.call_count, BROKER_MAX_ATTEMPTS - 1)

    @mock.patch('tap_salesforce.salesforce.token_broker.time.sleep')
    def test_fetch_broker_credentials_retries_timeout_then_succeeds(
            self, mock_sleep):
        response = mock.Mock()
        response.json.return_value = {
            'accessToken': 'sf-access',
            'instanceUrl': 'https://example.my.salesforce.com',
            'tokenVersion': 'v2',
        }
        response.raise_for_status = mock.Mock()

        session = mock.Mock()
        session.post.side_effect = [
            requests.exceptions.Timeout('timed out'),
            response,
        ]

        credentials = fetch_broker_credentials(
            endpoint='https://broker.example/token',
            reason='startup',
            task_auth_token='task-token',
            session=session)

        self.assertEqual(credentials['access_token'], 'sf-access')
        self.assertEqual(session.post.call_count, 2)
        mock_sleep.assert_called_once_with(1)

    @mock.patch('tap_salesforce.salesforce.token_broker.time.sleep')
    def test_fetch_broker_credentials_retries_lock_conflict_with_retry_after(
            self, mock_sleep):
        conflict_response = mock.Mock()
        conflict_response.status_code = 409
        conflict_response.headers = {'Retry-After': '2'}
        conflict_response.raise_for_status.side_effect = HTTPError(
            response=conflict_response)

        success_response = mock.Mock()
        success_response.json.return_value = {
            'accessToken': 'sf-access',
            'instanceUrl': 'https://example.my.salesforce.com',
            'tokenVersion': 'v2',
        }
        success_response.raise_for_status = mock.Mock()

        session = mock.Mock()
        session.post.side_effect = [conflict_response, success_response]

        credentials = fetch_broker_credentials(
            endpoint='https://broker.example/token',
            reason='periodic',
            task_auth_token='task-token',
            session=session)

        self.assertEqual(credentials['token_version'], 'v2')
        self.assertEqual(session.post.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @mock.patch('tap_salesforce.salesforce.token_broker.time.sleep')
    def test_fetch_broker_credentials_does_not_retry_auth_failure(
            self, mock_sleep):
        unauthorized_response = mock.Mock()
        unauthorized_response.status_code = 401
        unauthorized_response.headers = {}
        unauthorized_response.raise_for_status.side_effect = HTTPError(
            response=unauthorized_response)

        session = mock.Mock()
        session.post.return_value = unauthorized_response

        with self.assertRaises(TokenBrokerError):
            fetch_broker_credentials(
                endpoint='https://broker.example/token',
                reason='startup',
                task_auth_token='task-token',
                session=session)

        session.post.assert_called_once()
        mock_sleep.assert_not_called()


class SalesforceBrokerModeTests(unittest.TestCase):
    def _broker_salesforce(self):
        return Salesforce(
            **_base_salesforce_kwargs(
                token_broker={
                    'endpoint': 'https://broker.example/token',
                    'connection_id': 'conn-123',
                    'task_auth_token': 'task-token',
                }))

    @mock.patch('tap_salesforce.salesforce.threading.Timer')
    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_startup_login_uses_broker(self, mock_fetch, mock_timer):
        mock_fetch.return_value = {
            'access_token': 'broker-access',
            'instance_url': 'https://broker-instance.salesforce.com',
            'token_version': 'v1',
            'refresh_check_after_seconds': 600,
        }
        timer_instance = mock.Mock()
        mock_timer.return_value = timer_instance

        sf = self._broker_salesforce()
        sf.login()

        mock_fetch.assert_called_once_with(
            endpoint='https://broker.example/token',
            reason='startup',
            task_auth_token='task-token',
            known_token_version=None,
            session=sf.session)
        self.assertEqual(sf.access_token, 'broker-access')
        self.assertEqual(sf.instance_url,
                         'https://broker-instance.salesforce.com')
        self.assertEqual(sf.token_version, 'v1')
        mock_timer.assert_called_once_with(600, sf._on_login_timer)
        timer_instance.start.assert_called_once()

    @mock.patch('tap_salesforce.salesforce.threading.Timer')
    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_periodic_renewal_uses_broker(self, mock_fetch, mock_timer):
        mock_fetch.return_value = {
            'access_token': 'broker-access',
            'instance_url': 'https://broker-instance.salesforce.com',
            'token_version': 'v1',
            'refresh_check_after_seconds': None,
        }
        mock_timer.return_value = mock.Mock()

        sf = self._broker_salesforce()
        sf._on_login_timer()

        mock_fetch.assert_called_once_with(
            endpoint='https://broker.example/token',
            reason='periodic',
            task_auth_token='task-token',
            known_token_version=None,
            session=sf.session)

    @mock.patch('tap_salesforce.salesforce.threading.Timer')
    @mock.patch('tap_salesforce.salesforce.Salesforce._make_request')
    def test_legacy_mode_unchanged_without_broker_config(self, mock_make_request, mock_timer):
        auth_response = mock.Mock()
        auth_response.json.return_value = {
            'access_token': 'legacy-access',
            'instance_url': 'https://legacy.salesforce.com',
        }
        mock_make_request.return_value = auth_response
        mock_timer.return_value = mock.Mock()

        sf = Salesforce(**_base_salesforce_kwargs())
        sf.login()

        login_url = mock_make_request.call_args[0][1]
        self.assertIn('/services/oauth2/token', login_url)
        self.assertFalse(sf._broker_mode)
        self.assertEqual(sf.access_token, 'legacy-access')
        mock_timer.assert_called_once_with(
            REFRESH_TOKEN_EXPIRATION_PERIOD,
            sf._on_login_timer)

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_invalid_session_retries_get_once(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials('old-access', 'https://instance.salesforce.com', 'v-old')

        invalid_response = mock.Mock()
        invalid_response.json.return_value = [{
            'errorCode': 'INVALID_SESSION_ID',
            'message': 'Session expired or invalid',
        }]
        invalid_response.text = json.dumps(invalid_response.json.return_value)
        invalid_response.raise_for_status.side_effect = HTTPError(response=invalid_response)

        success_response = mock.Mock()
        success_response.headers = {}
        success_response.raise_for_status = mock.Mock()

        mock_fetch.return_value = {
            'access_token': 'new-access',
            'instance_url': 'https://instance.salesforce.com',
            'token_version': 'v-new',
            'refresh_check_after_seconds': 900,
        }

        with mock.patch.object(sf.session, 'get', side_effect=[invalid_response, success_response]) as mock_get:
            with mock.patch.object(sf, '_schedule_login_timer'):
                response = sf._make_request(
                    'GET',
                    'https://instance.salesforce.com/services/data/v52.0/queryAll',
                    headers={'Authorization': 'Bearer old-access'})

        self.assertIs(response, success_response)
        self.assertEqual(mock_get.call_count, 2)
        retry_headers = mock_get.call_args_list[1].kwargs['headers']
        self.assertEqual(retry_headers['Authorization'], 'Bearer new-access')
        self.assertEqual(retry_headers['X-SFDC-Session'], 'new-access')
        mock_fetch.assert_called_once_with(
            endpoint='https://broker.example/token',
            reason='invalid_session',
            task_auth_token='task-token',
            known_token_version='v-old',
            session=sf.session)

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_invalid_session_retries_post_once_on_explicit_error(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials('old-access', 'https://instance.salesforce.com', 'v-old')

        invalid_response = mock.Mock()
        invalid_response.json.return_value = [{
            'errorCode': 'INVALID_SESSION_ID',
            'message': 'Session expired or invalid',
        }]
        invalid_response.text = json.dumps(invalid_response.json.return_value)
        invalid_response.raise_for_status.side_effect = HTTPError(response=invalid_response)

        success_response = mock.Mock()
        success_response.headers = {}
        success_response.raise_for_status = mock.Mock()

        mock_fetch.return_value = {
            'access_token': 'new-access',
            'instance_url': 'https://instance.salesforce.com',
            'token_version': 'v-new',
            'refresh_check_after_seconds': 900,
        }

        with mock.patch.object(sf.session, 'post', side_effect=[invalid_response, success_response]) as mock_post:
            with mock.patch.object(sf, '_schedule_login_timer'):
                response = sf._make_request(
                    'POST',
                    'https://instance.salesforce.com/services/async/52.0/job',
                    headers={'X-SFDC-Session': 'old-access'},
                    body='{"operation":"queryAll"}')

        self.assertIs(response, success_response)
        self.assertEqual(mock_post.call_count, 2)
        retry_headers = mock_post.call_args_list[1].kwargs['headers']
        self.assertEqual(retry_headers['Authorization'], 'Bearer new-access')
        self.assertEqual(retry_headers['X-SFDC-Session'], 'new-access')
        mock_fetch.assert_called_once()

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_invalid_session_does_not_retry_network_failures(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials('old-access', 'https://instance.salesforce.com', 'v-old')
        make_request = getattr(sf._make_request, '__wrapped__', sf._make_request)

        with mock.patch.object(
                sf.session,
                'get',
                side_effect=requests.exceptions.ConnectionError('network down')) as mock_get:
            with self.assertRaises(requests.exceptions.ConnectionError):
                make_request(
                    sf,
                    'GET',
                    'https://instance.salesforce.com/services/data/v52.0/queryAll',
                    headers={'Authorization': 'Bearer old-access'})

        mock_get.assert_called_once()
        mock_fetch.assert_not_called()

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_invalid_session_does_not_retry_uncertain_failures(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials('old-access', 'https://instance.salesforce.com', 'v-old')
        make_request = getattr(sf._make_request, '__wrapped__', sf._make_request)

        invalid_response = mock.Mock()
        invalid_response.json.side_effect = ValueError('not json')
        invalid_response.text = 'INVALID_SESSION_ID in plain text only'
        invalid_response.raise_for_status.side_effect = HTTPError(response=invalid_response)

        with mock.patch.object(sf.session, 'get', return_value=invalid_response) as mock_get:
            with self.assertRaises(HTTPError):
                make_request(
                    sf,
                    'GET',
                    'https://instance.salesforce.com/services/data/v52.0/queryAll',
                    headers={'Authorization': 'Bearer old-access'})

        mock_get.assert_called_once()
        mock_fetch.assert_not_called()

    @mock.patch('tap_salesforce.salesforce.threading.Timer')
    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_startup_failure_does_not_schedule_timer(self, mock_fetch, mock_timer):
        mock_fetch.side_effect = TokenBrokerError('Token broker request failed')

        sf = self._broker_salesforce()
        with self.assertRaises(Exception):
            sf.login()

        mock_timer.assert_not_called()

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_concurrent_broker_login_is_serialized(self, mock_fetch):
        credentials = {
            'access_token': 'broker-access',
            'instance_url': 'https://broker-instance.salesforce.com',
            'token_version': 'v1',
            'refresh_check_after_seconds': 600,
        }
        active_fetches = []
        fetch_state_lock = threading.Lock()
        observed_max_active = {'value': 0}

        def tracked_fetch(**_kwargs):
            with fetch_state_lock:
                active_fetches.append(threading.current_thread().ident)
                observed_max_active['value'] = max(
                    observed_max_active['value'],
                    len(active_fetches))
            threading.Event().wait(0.1)
            with fetch_state_lock:
                active_fetches.remove(threading.current_thread().ident)
            return credentials

        mock_fetch.side_effect = tracked_fetch

        sf = self._broker_salesforce()
        errors = []

        def run_login():
            try:
                with mock.patch.object(sf, '_schedule_login_timer'):
                    sf._login_broker(reason='invalid_session')
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=run_login) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(observed_max_active['value'], 1)

    def test_token_replacement_is_thread_safe(self):
        sf = self._broker_salesforce()
        sf._set_session_credentials('token-a', 'https://a.salesforce.com', 'v-a')

        barrier = threading.Barrier(2)
        observed = []

        def reader():
            barrier.wait()
            for _ in range(100):
                observed.append((sf.access_token, sf.instance_url, sf.token_version))

        def writer():
            barrier.wait()
            sf._set_session_credentials('token-b', 'https://b.salesforce.com', 'v-b')

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)
        reader_thread.start()
        writer_thread.start()
        reader_thread.join()
        writer_thread.join()

        self.assertIn(('token-b', 'https://b.salesforce.com', 'v-b'), observed)
        self.assertEqual(sf.access_token, 'token-b')
        self.assertEqual(sf.instance_url, 'https://b.salesforce.com')
        self.assertEqual(sf.token_version, 'v-b')


class ConfigValidationTests(unittest.TestCase):
    @staticmethod
    def _legacy_config(**overrides):
        config = {
            'start_date': '2020-01-01T00:00:00Z',
            'api_type': 'REST',
            'select_fields_by_default': True,
            'source_type': 'object',
            'object_name': 'Account',
            'refresh_token': 'legacy-refresh',
            'client_id': 'legacy-client',
            'client_secret': 'legacy-secret',
        }
        config.update(overrides)
        return config

    def test_broker_mode_does_not_require_legacy_auth_keys(self):
        config = {
            'start_date': '2020-01-01T00:00:00Z',
            'api_type': 'REST',
            'select_fields_by_default': True,
            'source_type': 'object',
            'object_name': 'Account',
            'token_broker': {
                'endpoint': 'https://broker.example/token',
                'connection_id': 'conn-123',
                'task_auth_token': 'task-token',
            },
        }
        validate_config(config)

    def test_broker_mode_requires_connection_id(self):
        config = {
            'start_date': '2020-01-01T00:00:00Z',
            'api_type': 'REST',
            'select_fields_by_default': True,
            'source_type': 'object',
            'object_name': 'Account',
            'token_broker': {
                'endpoint': 'https://broker.example/token',
                'task_auth_token': 'task-token',
            },
        }
        with self.assertRaisesRegex(Exception, 'connection_id'):
            validate_config(config)

    def test_broker_mode_requires_task_auth_token(self):
        config = {
            'start_date': '2020-01-01T00:00:00Z',
            'api_type': 'REST',
            'select_fields_by_default': True,
            'source_type': 'object',
            'object_name': 'Account',
            'token_broker': {
                'endpoint': 'https://broker.example/token',
                'connection_id': 'conn-123',
            },
        }
        with self.assertRaisesRegex(Exception, 'task_auth_token'):
            validate_config(config)

    def test_legacy_mode_requires_refresh_and_client_keys(self):
        config = {
            'start_date': '2020-01-01T00:00:00Z',
            'api_type': 'REST',
            'select_fields_by_default': True,
            'source_type': 'object',
            'object_name': 'Account',
        }
        with self.assertRaisesRegex(Exception, 'refresh_token'):
            validate_config(config)

    def test_empty_broker_config_preserves_legacy_mode(self):
        validate_config(self._legacy_config(token_broker={}))

    def test_null_broker_config_preserves_legacy_mode(self):
        validate_config(self._legacy_config(token_broker=None))

    def test_non_object_broker_config_is_rejected_clearly(self):
        with self.assertRaisesRegex(Exception, 'must be an object'):
            validate_config(self._legacy_config(token_broker='invalid'))

    def test_partial_broker_config_does_not_silently_select_legacy_mode(self):
        with self.assertRaisesRegex(Exception, 'endpoint'):
            validate_config(self._legacy_config(
                token_broker={'connection_id': 'conn-123'}))

    def test_whitespace_broker_endpoint_is_rejected(self):
        with self.assertRaisesRegex(Exception, 'endpoint'):
            validate_config(self._legacy_config(token_broker={
                'endpoint': '   ',
                'connection_id': 'conn-123',
                'task_auth_token': 'task-token',
            }))


class TokenBrokerResponseTests(unittest.TestCase):
    def test_parse_broker_response_maps_camel_case_fields(self):
        parsed = parse_broker_response({
            'accessToken': 'abc',
            'instanceUrl': 'https://example.salesforce.com',
            'tokenVersion': 'v3',
            'refreshCheckAfterSeconds': '450',
        })
        self.assertEqual(parsed['access_token'], 'abc')
        self.assertEqual(parsed['refresh_check_after_seconds'], 450)


if __name__ == '__main__':
    unittest.main()
