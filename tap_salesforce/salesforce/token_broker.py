import json
import os

import requests
from requests.exceptions import RequestException

TASK_AUTH_TOKEN_ENV_VAR = 'SYMON_TASK_AUTH_TOKEN'
BROKER_REQUEST_TIMEOUT_SECONDS = 60
BROKER_REASONS = frozenset({'startup', 'periodic', 'invalid_session'})


class TokenBrokerError(Exception):
    """Raised when token broker authentication fails."""


def get_task_auth_token():
    return os.environ.get(TASK_AUTH_TOKEN_ENV_VAR)


def build_broker_request(endpoint, reason, known_token_version=None):
    if reason not in BROKER_REASONS:
        raise TokenBrokerError("Invalid broker reason: {}".format(reason))

    payload = {'reason': reason}
    if known_token_version is not None:
        payload['knownTokenVersion'] = known_token_version

    return {
        'url': endpoint,
        'headers': {
            'Content-Type': 'application/json',
        },
        'body': json.dumps(payload),
    }


def parse_broker_response(response_json):
    required_fields = ('accessToken', 'instanceUrl', 'tokenVersion')
    missing = [field for field in required_fields if field not in response_json]
    if missing:
        raise TokenBrokerError(
            "Token broker response missing required fields: {}".format(missing))

    refresh_check_after_seconds = response_json.get('refreshCheckAfterSeconds')
    if refresh_check_after_seconds is not None:
        refresh_check_after_seconds = int(refresh_check_after_seconds)

    return {
        'access_token': response_json['accessToken'],
        'instance_url': response_json['instanceUrl'],
        'token_version': response_json['tokenVersion'],
        'refresh_check_after_seconds': refresh_check_after_seconds,
    }


def fetch_broker_credentials(endpoint,
                             reason,
                             task_auth_token,
                             known_token_version=None,
                             session=None):
    if not task_auth_token:
        raise TokenBrokerError(
            "{} environment variable is required".format(TASK_AUTH_TOKEN_ENV_VAR))

    request = build_broker_request(
        endpoint,
        reason,
        known_token_version=known_token_version)
    headers = dict(request['headers'])
    headers['Authorization'] = 'TaskAuth {}'.format(task_auth_token)

    http = session or requests
    try:
        resp = http.post(
            request['url'],
            headers=headers,
            data=request['body'],
            timeout=BROKER_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except RequestException:
        raise TokenBrokerError("Token broker request failed")

    try:
        return parse_broker_response(resp.json())
    except (ValueError, TypeError) as exc:
        raise TokenBrokerError("Token broker returned invalid JSON") from exc
