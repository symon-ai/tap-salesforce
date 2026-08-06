import json
import unittest
from unittest import mock

import requests
from requests.exceptions import HTTPError

from tap_salesforce import validate_config
from tap_salesforce.salesforce import (
    BROKER_REFRESH_CHECK_AFTER_SECONDS,
    Salesforce,
)
from tap_salesforce.salesforce.rest import Rest
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
        'default_start_date': '2020-01-01T00:00:00Z',
        'source_type': 'object',
        'object_name': 'Account',
        'token_broker': {
            'endpoint': 'https://broker.example/token',
            'connection_id': 'conn-123',
            'task_auth_token': 'task-token',
        },
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

    @mock.patch('tap_salesforce.salesforce.token_broker.LOGGER')
    @mock.patch('tap_salesforce.salesforce.token_broker.time.sleep')
    def test_fetch_broker_credentials_retries_timeout_then_succeeds(
            self, mock_sleep, mock_logger):
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
        self.assertEqual(
            mock_logger.info.call_args_list,
            [
                mock.call(
                    "Token broker request attempt %s/%s (%s)",
                    1,
                    BROKER_MAX_ATTEMPTS,
                    'startup'),
                mock.call(
                    "Token broker request attempt %s/%s (%s)",
                    2,
                    BROKER_MAX_ATTEMPTS,
                    'startup'),
                mock.call(
                    "Token broker request attempt %s/%s succeeded",
                    2,
                    BROKER_MAX_ATTEMPTS),
            ])
        mock_logger.warning.assert_called_once_with(
            "Token broker request attempt %s/%s failed (%s); "
            "retrying in %s seconds",
            1,
            BROKER_MAX_ATTEMPTS,
            'Timeout',
            1)

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

    @mock.patch('tap_salesforce.salesforce.token_broker.LOGGER')
    @mock.patch('tap_salesforce.salesforce.token_broker.time.sleep')
    def test_fetch_broker_credentials_does_not_retry_auth_failure(
            self, mock_sleep, mock_logger):
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
        mock_logger.error.assert_called_once_with(
            "Token broker request attempt %s/%s failed (%s); %s",
            1,
            BROKER_MAX_ATTEMPTS,
            'HTTP 401',
            'not retryable')
        self.assertNotIn('task-token', str(mock_logger.mock_calls))


class SalesforceBrokerModeTests(unittest.TestCase):
    def _broker_salesforce(self):
        return Salesforce(**_base_salesforce_kwargs())

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_startup_login_uses_broker(self, mock_fetch):
        mock_fetch.return_value = {
            'access_token': 'broker-access',
            'instance_url': 'https://broker-instance.salesforce.com',
            'token_version': 'v1',
        }

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
        self.assertEqual(
            sf.refresh_check_after_seconds,
            BROKER_REFRESH_CHECK_AFTER_SECONDS)
        self.assertIsNotNone(sf._last_broker_check_at)

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_request_skips_periodic_validation_before_interval(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials(
            'current-access', 'https://instance.salesforce.com', 'v-current')
        sf.refresh_check_after_seconds = 900
        sf._last_broker_check_at = 100

        success_response = mock.Mock()
        success_response.headers = {}
        success_response.raise_for_status = mock.Mock()

        headers = {'Authorization': 'Bearer current-access'}
        with mock.patch(
                'tap_salesforce.salesforce.time.monotonic',
                return_value=999):
            with mock.patch.object(
                    sf.session,
                    'get',
                    return_value=success_response) as mock_get:
                sf._make_request(
                    'GET',
                    'https://instance.salesforce.com/services/data/v52.0/queryAll',
                    headers=headers)

        mock_fetch.assert_not_called()
        self.assertEqual(
            mock_get.call_args.kwargs['headers']['Authorization'],
            'Bearer current-access')

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_request_validates_due_token_before_sending(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials(
            'old-access', 'https://instance.salesforce.com', 'v-old')
        sf.refresh_check_after_seconds = 900
        sf._last_broker_check_at = 100

        mock_fetch.return_value = {
            'access_token': 'new-access',
            'instance_url': 'https://new-instance.salesforce.com',
            'token_version': 'v-new',
        }
        success_response = mock.Mock()
        success_response.headers = {}
        success_response.raise_for_status = mock.Mock()

        headers = {'Authorization': 'Bearer old-access'}
        with mock.patch(
                'tap_salesforce.salesforce.time.monotonic',
                return_value=1000):
            with mock.patch.object(
                    sf.session,
                    'get',
                    return_value=success_response) as mock_get:
                sf._make_request(
                    'GET',
                    'https://instance.salesforce.com/services/data/v52.0/queryAll',
                    headers=headers)

        mock_fetch.assert_called_once_with(
            endpoint='https://broker.example/token',
            reason='periodic',
            task_auth_token='task-token',
            known_token_version='v-old',
            session=sf.session)
        self.assertEqual(headers['Authorization'], 'Bearer new-access')
        self.assertEqual(
            mock_get.call_args.kwargs['headers']['Authorization'],
            'Bearer new-access')
        self.assertEqual(
            mock_get.call_args.args[0],
            'https://new-instance.salesforce.com/services/data/v52.0/queryAll')

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_periodic_validation_failure_continues_with_current_token(
            self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials(
            'current-access', 'https://instance.salesforce.com', 'v-current')
        sf.refresh_check_after_seconds = 900
        sf._last_broker_check_at = 100
        mock_fetch.side_effect = TokenBrokerError(
            'Token broker request failed')

        success_response = mock.Mock()
        success_response.headers = {}
        success_response.raise_for_status = mock.Mock()

        headers = {'Authorization': 'Bearer current-access'}
        with mock.patch(
                'tap_salesforce.salesforce.time.monotonic',
                side_effect=[1000, 1015]):
            with mock.patch.object(
                    sf.session,
                    'get',
                    return_value=success_response) as mock_get:
                sf._make_request(
                    'GET',
                    'https://instance.salesforce.com/services/data/v52.0/queryAll',
                    headers=headers)

        self.assertEqual(
            mock_get.call_args.kwargs['headers']['Authorization'],
            'Bearer current-access')
        self.assertEqual(sf._last_broker_check_at, 1015)

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
            'instance_url': 'https://new-instance.salesforce.com',
            'token_version': 'v-new',
        }

        with mock.patch.object(sf.session, 'get', side_effect=[invalid_response, success_response]) as mock_get:
            response = sf._make_request(
                'GET',
                'https://instance.salesforce.com/services/data/v52.0/queryAll',
                headers={'Authorization': 'Bearer old-access'})

        self.assertIs(response, success_response)
        self.assertEqual(mock_get.call_count, 2)
        retry_headers = mock_get.call_args_list[1].kwargs['headers']
        self.assertEqual(retry_headers['Authorization'], 'Bearer new-access')
        self.assertEqual(retry_headers['X-SFDC-Session'], 'new-access')
        self.assertEqual(
            mock_get.call_args_list[1].args[0],
            'https://new-instance.salesforce.com/services/data/v52.0/queryAll')
        mock_fetch.assert_called_once_with(
            endpoint='https://broker.example/token',
            reason='invalid_session',
            task_auth_token='task-token',
            known_token_version='v-old',
            session=sf.session)

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_invalid_session_updates_headers_for_later_rest_pages(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials(
            'old-access', 'https://instance.salesforce.com', 'v-old')

        invalid_response = mock.Mock()
        invalid_response.json.return_value = [{
            'errorCode': 'INVALID_SESSION_ID',
            'message': 'Session expired or invalid',
        }]
        invalid_response.raise_for_status.side_effect = HTTPError(
            response=invalid_response)

        first_page = mock.Mock()
        first_page.headers = {}
        first_page.raise_for_status = mock.Mock()
        first_page.json.return_value = {
            'records': [{'Id': 'first'}],
            'nextRecordsUrl': '/services/data/v52.0/query/next',
        }

        second_page = mock.Mock()
        second_page.headers = {}
        second_page.raise_for_status = mock.Mock()
        second_page.json.return_value = {
            'records': [{'Id': 'second'}],
            'nextRecordsUrl': None,
        }

        mock_fetch.return_value = {
            'access_token': 'new-access',
            'instance_url': 'https://instance.salesforce.com',
            'token_version': 'v-new',
        }

        headers = {'Authorization': 'Bearer old-access'}
        with mock.patch.object(
                sf.session,
                'get',
                side_effect=[invalid_response, first_page, second_page]) as mock_get:
            records = list(Rest(sf)._sync_records(
                'https://instance.salesforce.com/services/data/v52.0/queryAll',
                headers,
                {'q': 'SELECT Id FROM Account'}))

        self.assertEqual(records, [{'Id': 'first'}, {'Id': 'second'}])
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(headers['Authorization'], 'Bearer new-access')
        self.assertEqual(
            mock_get.call_args_list[2].kwargs['headers']['Authorization'],
            'Bearer new-access')
        mock_fetch.assert_called_once()

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
        }

        with mock.patch.object(sf.session, 'post', side_effect=[invalid_response, success_response]) as mock_post:
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
        invalid_response.text = 'unrecognized server error'
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

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_bulk_xml_invalid_session_retries_once(self, mock_fetch):
        sf = self._broker_salesforce()
        sf._set_session_credentials(
            'old-access', 'https://instance.salesforce.com', 'v-old')

        invalid_response = mock.Mock()
        invalid_response.status_code = 400
        invalid_response.json.side_effect = ValueError('not json')
        invalid_response.text = (
            '<error><exceptionCode>InvalidSessionId</exceptionCode></error>')
        invalid_response.raise_for_status.side_effect = HTTPError(
            response=invalid_response)

        success_response = mock.Mock()
        success_response.headers = {}
        success_response.raise_for_status = mock.Mock()

        mock_fetch.return_value = {
            'access_token': 'new-access',
            'instance_url': 'https://instance.salesforce.com',
            'token_version': 'v-new',
        }

        with mock.patch.object(
                sf.session,
                'get',
                side_effect=[invalid_response, success_response]) as mock_get:
            response = sf._make_request(
                'GET',
                'https://instance.salesforce.com/services/async/52.0/job/123',
                headers={'X-SFDC-Session': 'old-access'})

        self.assertIs(response, success_response)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(
            mock_get.call_args_list[1].kwargs['headers']['X-SFDC-Session'],
            'new-access')
        mock_fetch.assert_called_once()

    @mock.patch('tap_salesforce.salesforce.fetch_broker_credentials')
    def test_startup_failure_is_reported(self, mock_fetch):
        mock_fetch.side_effect = TokenBrokerError('Token broker request failed')

        sf = self._broker_salesforce()
        with self.assertRaises(Exception):
            sf.login()


class ConfigValidationTests(unittest.TestCase):
    @staticmethod
    def _base_config(**overrides):
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
        config.update(overrides)
        return config

    def test_valid_broker_config(self):
        validate_config(self._base_config())

    def test_requires_connection_id(self):
        with self.assertRaisesRegex(Exception, 'connection_id'):
            validate_config(self._base_config(token_broker={
                'endpoint': 'https://broker.example/token',
                'task_auth_token': 'task-token',
            }))

    def test_requires_task_auth_token(self):
        with self.assertRaisesRegex(Exception, 'task_auth_token'):
            validate_config(self._base_config(token_broker={
                'endpoint': 'https://broker.example/token',
                'connection_id': 'conn-123',
            }))

    def test_requires_token_broker(self):
        config = self._base_config()
        del config['token_broker']
        with self.assertRaisesRegex(Exception, 'token_broker'):
            validate_config(config)

    def test_empty_broker_config_is_rejected(self):
        with self.assertRaisesRegex(Exception, 'endpoint'):
            validate_config(self._base_config(token_broker={}))

    def test_null_broker_config_is_rejected(self):
        with self.assertRaisesRegex(Exception, 'must be an object'):
            validate_config(self._base_config(token_broker=None))

    def test_legacy_credentials_do_not_replace_broker_config(self):
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
        with self.assertRaisesRegex(Exception, 'token_broker'):
            validate_config(config)

    def test_non_object_broker_config_is_rejected_clearly(self):
        with self.assertRaisesRegex(Exception, 'must be an object'):
            validate_config(self._base_config(token_broker='invalid'))

    def test_partial_broker_config_is_rejected(self):
        with self.assertRaisesRegex(Exception, 'endpoint'):
            validate_config(self._base_config(
                token_broker={'connection_id': 'conn-123'}))

    def test_whitespace_broker_endpoint_is_rejected(self):
        with self.assertRaisesRegex(Exception, 'endpoint'):
            validate_config(self._base_config(token_broker={
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
        })
        self.assertEqual(parsed['access_token'], 'abc')
        self.assertEqual(
            parsed['instance_url'],
            'https://example.salesforce.com')
        self.assertEqual(parsed['token_version'], 'v3')


if __name__ == '__main__':
    unittest.main()
