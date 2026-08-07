import json
import logging
import time

import requests
from requests.exceptions import RequestException

LOGGER = logging.getLogger(__name__)

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

    return {
        'access_token': response_json['accessToken'],
        'instance_url': response_json['instanceUrl'],
        'token_version': response_json['tokenVersion'],
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
        attempt_number = attempt + 1
        LOGGER.info(
            "Token broker request attempt %s/%s (%s)",
            attempt_number,
            BROKER_MAX_ATTEMPTS,
            reason)
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
                attempt_number < BROKER_MAX_ATTEMPTS
                and _is_retryable_broker_failure(error_response)
            )
            failure = (
                "HTTP {}".format(error_response.status_code)
                if error_response is not None
                else type(exc).__name__
            )
            if not can_retry:
                terminal_reason = (
                    "no retries remain"
                    if attempt_number >= BROKER_MAX_ATTEMPTS
                    else "not retryable"
                )
                LOGGER.error(
                    "Token broker request attempt %s/%s failed (%s); %s",
                    attempt_number,
                    BROKER_MAX_ATTEMPTS,
                    failure,
                    terminal_reason)
                raise TokenBrokerError("Token broker request failed") from exc

            retry_delay = _get_retry_delay_seconds(error_response, attempt)
            LOGGER.warning(
                "Token broker request attempt %s/%s failed (%s); "
                "retrying in %s seconds",
                attempt_number,
                BROKER_MAX_ATTEMPTS,
                failure,
                retry_delay)
            time.sleep(retry_delay)
            continue

        try:
            credentials = parse_broker_response(resp.json())
        except (ValueError, TypeError) as exc:
            LOGGER.error(
                "Token broker request attempt %s/%s returned an invalid response",
                attempt_number,
                BROKER_MAX_ATTEMPTS)
            raise TokenBrokerError(
                "Token broker returned invalid JSON") from exc
        except TokenBrokerError:
            LOGGER.error(
                "Token broker request attempt %s/%s returned an invalid response",
                attempt_number,
                BROKER_MAX_ATTEMPTS)
            raise

        LOGGER.info(
            "Token broker request attempt %s/%s succeeded",
            attempt_number,
            BROKER_MAX_ATTEMPTS)
        return credentials

    raise TokenBrokerError("Token broker request failed")
