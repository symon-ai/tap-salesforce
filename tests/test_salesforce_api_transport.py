import json
import socket
import unittest
from unittest.mock import patch

from requests import Response

from tap_salesforce.salesforce import Salesforce
from tap_salesforce.salesforce.broker_api import BrokerAPIError
from tap_salesforce.salesforce.rest import Rest


def response(status, payload=None, **headers):
    result = Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    result.headers.update(headers)
    return result


class SalesforceAPITransportTests(unittest.TestCase):
    def setUp(self):
        self.rpc = 'http://broker.internal/token'
        self.old = 'https://api.example.com/old'
        self.new = 'https://api.example.com/new/deep'
        self.endpoint = '/services/data/v52.0/queryAll'
        self.sf = Salesforce(default_start_date='2020-01-01T00:00:00Z',
                             source_type='object', object_name='Account',
                             token_broker={'endpoint': self.rpc, 'task_auth_token': 'task-token'})

    @staticmethod
    def credentials(token, base, version):
        return response(200, {'accessToken': token, 'instanceUrl': base, 'tokenVersion': version})

    def test_periodic_refresh_rebases_original_prefix_before_request(self):
        with patch('requests.adapters.HTTPAdapter.send', side_effect=[
                self.credentials('old', self.old + '/', '1'),
                self.credentials('new', self.new, '2'),
                response(200),
        ]) as send:
            self.sf.login()
            self.sf.refresh_check_after_seconds = 0
            self.sf._make_request('GET', self.sf.instance_url + self.endpoint + '?page=1',
                                  self.sf._get_standard_headers())
        startup, refresh, actual = [call.args[0] for call in send.call_args_list]
        self.assertEqual([startup.url, refresh.url, actual.url],
                         [self.rpc, self.rpc, self.new + self.endpoint + '?page=1'])
        self.assertEqual(json.loads(refresh.body), {'reason': 'periodic', 'knownTokenVersion': '1'})
        self.assertEqual(actual.headers['Authorization'], 'Bearer new')

    def test_invalid_session_refresh_and_both_cursor_forms_keep_query(self):
        cursor = '/services/data/v52.0/query/cursor'
        for prefix in ('', '/new/deep'):
            with self.subTest(prefix=prefix):
                self.sf._set_session_credentials('old', self.old, '1')
                with patch('requests.adapters.HTTPAdapter.send', side_effect=[
                        response(401, [{'errorCode': 'INVALID_SESSION_ID'}]),
                        self.credentials('new', self.new, '2'),
                        response(200, {'records': [{'Id': '1'}],
                                       'nextRecordsUrl': prefix + cursor + '?after=a%2Fb&n=10'}),
                        response(200, {'records': [{'Id': '2'}]}),
                ]) as send:
                    records = list(Rest(self.sf)._sync_records(
                        self.old + self.endpoint, self.sf._get_standard_headers(),
                        {'q': 'SELECT Id FROM Account'}))
                self.assertEqual(records, [{'Id': '1'}, {'Id': '2'}])
                expired, refresh, actual, next_page = [call.args[0] for call in send.call_args_list]
                self.assertTrue(expired.url.startswith(self.old + self.endpoint + '?q='))
                self.assertEqual(refresh.url, self.rpc)
                self.assertEqual(json.loads(refresh.body),
                                 {'reason': 'invalid_session', 'knownTokenVersion': '1'})
                self.assertTrue(actual.url.startswith(self.new + self.endpoint + '?q='))
                self.assertEqual(next_page.url, self.new + cursor + '?after=a%2Fb&n=10')
                self.assertEqual(next_page.headers['Authorization'], 'Bearer new')

    def test_broker_api_rejects_redirect_and_blocks_nonpublic_dns_at_connect(self):
        self.sf._set_session_credentials('old', self.old, '1')
        with patch('requests.adapters.HTTPAdapter.send', return_value=response(
                307, Location='https://elsewhere.example.com/')) as send:
            with self.assertRaisesRegex(BrokerAPIError, 'returned a redirect'):
                self.sf._make_request('POST', self.old + '/services/async/52.0/job',
                                      {'X-SFDC-Session': 'old'})
            send.assert_called_once()
            self.assertTrue(send.call_args.kwargs['verify'])
        with patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::ffff:127.0.0.1', 443, 0, 0)),
        ]) as resolver, patch('socket.socket') as make_socket:
            with self.assertRaisesRegex(BrokerAPIError, 'public address'):
                self.sf._make_request('GET', self.old + self.endpoint, self.sf._get_standard_headers())
            resolver.assert_called_once_with('api.example.com', 443, socket.AF_UNSPEC,
                                             socket.SOCK_STREAM)
            make_socket.assert_not_called()

    def test_checked_public_sockaddr_is_used_without_resolving_again(self):
        self.sf._set_session_credentials('old', self.old, '1')
        public = ('93.184.216.34', 443)
        with patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', public),
        ]) as resolver, patch('socket.socket') as make_socket, patch(
            'urllib3.connection._ssl_wrap_socket_and_match_hostname',
            side_effect=BrokerAPIError('TLS test boundary'),
        ) as tls:
            with self.assertRaisesRegex(BrokerAPIError, 'TLS test boundary'):
                self.sf._make_request('GET', self.old + self.endpoint, self.sf._get_standard_headers())
        resolver.assert_called_once()
        make_socket.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, 6)
        make_socket.return_value.connect.assert_called_once_with(public)
        self.assertEqual(tls.call_args.kwargs['server_hostname'], 'api.example.com')
        self.assertEqual(tls.call_args.kwargs['cert_reqs'], 'CERT_REQUIRED')
