import json
import time

import requests
from requests.exceptions import RequestException

BROKER_REQUEST_TIMEOUT_SECONDS = 60
BROKER_MAX_ATTEMPTS = 3
BROKER_MAX_RETRY_DELAY_SECONDS = 10
BROKER_RETRYABLE_STATUS_CODES = frozenset({
    408, 409, 425, 429, 500, 502, 503, 504,
})
BROKER_REASONS = frozenset({'startup', 'periodic', 'invalid_session'})


class TokenBrokerError(Exception):
    """Raised when token broker authentication fails."""


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


def _get_retry_delay_seconds(response, attempt):
    if response is not None:
        retry_after = response.headers.get('Retry-After')
        if retry_after is not None:
            try:
                return min(
                    max(float(retry_after), 0),
                    BROKER_MAX_RETRY_DELAY_SECONDS)
            except (TypeError, ValueError):
                pass

    return min(2 ** attempt, BROKER_MAX_RETRY_DELAY_SECONDS)


def _is_retryable_broker_failure(response):
    return response is None or response.status_code in BROKER_RETRYABLE_STATUS_CODES


def fetch_broker_credentials(endpoint,
                             reason,
                             task_auth_token,
                             known_token_version=None,
                             session=None):
    if not task_auth_token:
        raise TokenBrokerError("token_broker.task_auth_token is required")

    request = build_broker_request(
        endpoint,
        reason,
        known_token_version=known_token_version)
    headers = dict(request['headers'])
    headers['Authorization'] = 'TaskAuth {}'.format(task_auth_token)

    http = session or requests
    for attempt in range(BROKER_MAX_ATTEMPTS):
        resp = None
        try:
            resp = http.post(
                request['url'],
                headers=headers,
                data=request['body'],
                timeout=BROKER_REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
        except RequestException as exc:
            error_response = getattr(exc, 'response', None)
            if error_response is None:
                error_response = resp

            can_retry = (
                attempt + 1 < BROKER_MAX_ATTEMPTS
                and _is_retryable_broker_failure(error_response)
            )
            if not can_retry:
                raise TokenBrokerError("Token broker request failed") from exc

            time.sleep(_get_retry_delay_seconds(error_response, attempt))
            continue

        try:
            return parse_broker_response(resp.json())
        except (ValueError, TypeError) as exc:
            raise TokenBrokerError(
                "Token broker returned invalid JSON") from exc

    raise TokenBrokerError("Token broker request failed")
