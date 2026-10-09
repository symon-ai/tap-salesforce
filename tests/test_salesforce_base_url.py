import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import tap_salesforce
from tap_salesforce.salesforce.local_oauth import LOCAL_OAUTH_REQUEST_TIMEOUT_SECONDS


class BaseUrlEntrypointTests(unittest.TestCase):
    def _run_discovery(self, base_url, payloads, expected_hosts, sandbox=False,
                       broker=False, invalid_session=False):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                'start_date': '2020-01-01T00:00:00Z',
                'api_type': 'REST',
                'select_fields_by_default': True,
                'source_type': 'object',
                'object_name': 'Account',
                'auth_mode': 'local',
                'local_oauth': {
                    'client_id': 'client-id',
                    'client_secret': 'client-secret',
                    'refresh_token': 'refresh-token',
                    'is_sandbox': sandbox,
                    'refresh_token_log_path': str(Path(directory) / 'tokens.jsonl'),
                },
            }
            if base_url is not None:
                config['base_url'] = base_url
            if broker:
                config['token_broker'] = {
                    'endpoint': 'https://elt-broker.example/access-token',
                    'connection_id': 'connection-id',
                    'task_auth_token': 'task-token',
                }
                config['local_oauth'] = 'invalid-but-ignored'
            config_path = Path(directory) / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            session = mock.Mock(spec=requests.Session)
            responses = []
            for payload in payloads:
                response = mock.Mock()
                response.json.return_value = payload
                responses.append(response)
            session.post.side_effect = responses
            success = mock.Mock()
            success.headers = {}
            if invalid_session:
                unauthorized = mock.Mock()
                unauthorized.status_code = 401
                unauthorized.raise_for_status.side_effect = requests.HTTPError(
                    response=unauthorized)
                session.get.side_effect = [unauthorized, success]
            else:
                session.get.return_value = success

            def discover(sf):
                self.assertEqual(sf.instance_url, expected_hosts[0])
                url = sf.data_url.format(sf.instance_url, 'sobjects/Account/describe')
                sf._make_request('GET', url, headers=sf._get_standard_headers())
                if len(payloads) > 1 and not invalid_session:
                    sf._last_broker_check_at = 0
                    sf._make_request('GET', url, headers=sf._get_standard_headers())
                self.assertEqual(sf.instance_url, expected_hosts[-1])

            with mock.patch.object(tap_salesforce, 'CONFIG', {'start_date': None}), \
                    mock.patch.object(sys, 'argv', ['tap-salesforce', '--config',
                                                   str(config_path), '--discover']), \
                    mock.patch('tap_salesforce.salesforce.requests.Session',
                               return_value=session), \
                    mock.patch.object(tap_salesforce, 'do_discover', side_effect=discover):
                tap_salesforce.main_impl()

            endpoint = ('https://elt-broker.example/access-token' if broker else
                        (base_url or ('https://test.salesforce.com' if sandbox else
                                      'https://login.salesforce.com')).rstrip('/')
                        + '/services/oauth2/token')
            for call in session.post.call_args_list:
                self.assertEqual(call.args[0], endpoint)
                self.assertNotIn('verify', call.kwargs)
                if broker:
                    self.assertEqual(call.kwargs['headers']['Authorization'],
                                     'TaskAuth task-token')
                else:
                    self.assertEqual(call.kwargs['timeout'],
                                     LOCAL_OAUTH_REQUEST_TIMEOUT_SECONDS)
                    self.assertEqual(call.kwargs['data']['grant_type'], 'refresh_token')
            for call, host in zip(session.get.call_args_list, expected_hosts):
                self.assertEqual(call.args[0], host + '/services/data/v52.0/sobjects/Account/describe')
                self.assertNotIn('verify', call.kwargs)
            self.assertEqual(len(session.get.call_args_list), len(expected_hosts))
            return session

    def test_custom_base_and_service_url_route_through_entrypoint(self):
        self._run_discovery('https://customer.example/', [
            {'access_token': 'opaque-not-a-jwt', 'service_url': 'https://service.example'},
        ], ['https://service.example'])

    def test_instance_url_precedes_service_url_and_custom_fallback(self):
        self._run_discovery('https://customer.example', [
            {'access_token': 'opaque', 'instance_url': 'https://instance.example',
             'service_url': 'https://service.example'},
        ], ['https://instance.example'])

    def test_custom_base_routes_when_exchange_returns_no_url(self):
        self._run_discovery('https://customer.example/', [
            {'access_token': 'opaque'},
        ], ['https://customer.example'])

    def test_empty_and_omitted_base_preserve_production_and_sandbox_defaults(self):
        for base_url in (None, ''):
            for sandbox in (False, True):
                with self.subTest(base_url=base_url, sandbox=sandbox):
                    host = ('https://test.salesforce.com' if sandbox else
                            'https://login.salesforce.com')
                    self._run_discovery(base_url, [{'access_token': 'opaque'}],
                                        [host], sandbox=sandbox)

    def test_periodic_refresh_updates_host_and_header_but_not_exchange_endpoint(self):
        session = self._run_discovery('https://customer.example', [
            {'access_token': 'first-token', 'service_url': 'https://first.example'},
            {'access_token': 'second-token', 'service_url': 'https://second.example'},
        ], ['https://first.example', 'https://second.example'])
        self.assertEqual(session.get.call_args.kwargs['headers']['Authorization'],
                         'Bearer second-token')

    def test_invalid_session_refresh_updates_request_routing(self):
        session = self._run_discovery('https://customer.example', [
            {'access_token': 'first-token', 'instance_url': 'https://first.example'},
            {'access_token': 'second-token', 'service_url': 'https://second.example'},
        ], ['https://first.example', 'https://second.example'], invalid_session=True)
        self.assertEqual(session.get.call_args.kwargs['headers']['Authorization'],
                         'Bearer second-token')

    def test_broker_keeps_endpoint_authority_and_mode_precedence(self):
        self._run_discovery('https://customer.example', [
            {'accessToken': 'broker-token', 'instanceUrl': 'https://broker-route.example',
             'tokenVersion': '1'},
            {'accessToken': 'new-token', 'instanceUrl': 'https://new-broker-route.example',
             'tokenVersion': '2'},
        ], ['https://broker-route.example', 'https://new-broker-route.example'], broker=True)


if __name__ == '__main__':
    unittest.main()
