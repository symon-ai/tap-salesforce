"""Direct, public HTTPS transport for the Salesforce API, not the broker RPC."""

import socket
import sys
from ipaddress import ip_address, ip_network

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util import Timeout


class BrokerAPIError(Exception):
    """The Salesforce API request violated broker-mode transport policy."""


_GLOBAL_IPV6_UNICAST = ip_network('2000::/3')


def _is_public_address(sockaddr):
    address = ip_address(sockaddr[0])
    if address.version == 6:
        if sockaddr[3]:
            return False
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif (address not in _GLOBAL_IPV6_UNICAST
              or address.sixtofour is not None or address.teredo is not None):
            return False
    return address.is_global and not address.is_multicast


class _PublicHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        # Resolve when opening each socket and connect to the checked numeric
        # sockaddr directly. Keep urllib3's original hostname for Host and TLS.
        try:
            addresses = socket.getaddrinfo(
                self._dns_host.strip('[]'), self.port, socket.AF_UNSPEC,
                socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc

        last_error = None
        for family, socktype, proto, _, sockaddr in addresses:
            if family not in (socket.AF_INET, socket.AF_INET6) or not _is_public_address(sockaddr):
                continue
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.timeout is not Timeout.DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                sys.audit('http.client.connect', self, self.host, self.port)
                return sock
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
            except BaseException:
                if sock is not None:
                    sock.close()
                raise

        if isinstance(last_error, socket.timeout):
            raise ConnectTimeoutError(self, 'Salesforce API connection timed out') from last_error
        if last_error is not None:
            raise NewConnectionError(self, 'Salesforce API connection failed') from last_error
        raise BrokerAPIError('Salesforce API hostname did not resolve to a public address')


class _PublicHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class _PublicHTTPSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {'https': _PublicHTTPSConnectionPool}

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if not request.url.startswith('https://') or verify is not True or proxies:
            raise BrokerAPIError('Salesforce API requires direct HTTPS with TLS verification')
        return super().send(request, stream=stream, timeout=timeout, verify=verify,
                            cert=cert, proxies=proxies)


def configure_broker_api_session(session):
    session.trust_env = False
    session.adapters.clear()
    session.mount('https://', _PublicHTTPSAdapter())
