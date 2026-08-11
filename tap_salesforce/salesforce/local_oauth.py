import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests


LOGGER = logging.getLogger(__name__)

LOCAL_OAUTH_REQUEST_TIMEOUT_SECONDS = 60
LOCAL_OAUTH_REASONS = frozenset({'startup', 'periodic', 'invalid_session'})
DEFAULT_REFRESH_TOKEN_LOG_PATH = (
    '.salesforce-oauth/refresh-tokens.jsonl')


class LocalOAuthError(Exception):
    """Raised when local Salesforce OAuth authentication fails."""


def validate_local_oauth_config(local_oauth):
    if not isinstance(local_oauth, dict):
        raise LocalOAuthError('local_oauth must be an object')

    for field in ('client_id', 'client_secret', 'refresh_token'):
        value = local_oauth.get(field)
        if not isinstance(value, str) or not value.strip():
            raise LocalOAuthError(
                'local_oauth.{} is required'.format(field))

    is_sandbox = local_oauth.get('is_sandbox', False)
    if not isinstance(is_sandbox, bool):
        raise LocalOAuthError('local_oauth.is_sandbox must be a boolean')

    log_path = local_oauth.get(
        'refresh_token_log_path',
        DEFAULT_REFRESH_TOKEN_LOG_PATH)
    if not isinstance(log_path, str) or not log_path.strip():
        raise LocalOAuthError(
            'local_oauth.refresh_token_log_path must be a non-empty string')


class LocalOAuthClient:
    """Exchanges a local refresh token and preserves rotated replacements."""

    def __init__(self, local_oauth, session=None):
        validate_local_oauth_config(local_oauth)
        self.client_id = local_oauth['client_id']
        self.client_secret = local_oauth['client_secret']
        self.refresh_token = local_oauth['refresh_token']
        self.is_sandbox = local_oauth.get('is_sandbox', False)
        self.refresh_token_log_path = Path(local_oauth.get(
            'refresh_token_log_path',
            DEFAULT_REFRESH_TOKEN_LOG_PATH))
        self.session = session or requests
        self._prepare_refresh_token_log()

    @property
    def token_url(self):
        domain = 'test.salesforce.com' if self.is_sandbox else 'login.salesforce.com'
        return 'https://{}/services/oauth2/token'.format(domain)

    def fetch_credentials(self, reason):
        if reason not in LOCAL_OAUTH_REASONS:
            raise LocalOAuthError(
                'Invalid local OAuth reason: {}'.format(reason))

        exchanged_at = datetime.now(timezone.utc)
        try:
            response = self.session.post(
                self.token_url,
                data={
                    'grant_type': 'refresh_token',
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'refresh_token': self.refresh_token,
                },
                timeout=LOCAL_OAUTH_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            LOGGER.error(
                'Local OAuth exchange failed at %s '
                '(sandbox=%s, reason=%s)',
                self._format_timestamp(exchanged_at),
                self.is_sandbox,
                reason)
            raise LocalOAuthError('Local OAuth token exchange failed') from exc

        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise LocalOAuthError(
                'Local OAuth token endpoint returned invalid JSON') from exc

        access_token = payload.get('access_token')
        instance_url = payload.get('instance_url')
        if not isinstance(access_token, str) or not access_token.strip():
            raise LocalOAuthError(
                'Local OAuth response missing access_token')
        if not isinstance(instance_url, str) or not instance_url.strip():
            raise LocalOAuthError(
                'Local OAuth response missing instance_url')

        returned_refresh_token = payload.get('refresh_token')
        has_new_refresh_token = (
            isinstance(returned_refresh_token, str)
            and bool(returned_refresh_token.strip())
            and returned_refresh_token != self.refresh_token
        )
        if has_new_refresh_token:
            self.refresh_token = returned_refresh_token
            self._append_refresh_token(
                returned_refresh_token,
                exchanged_at)

        LOGGER.info(
            'Local OAuth exchange completed at %s '
            '(sandbox=%s, reason=%s, new_refresh_token_returned=%s)',
            self._format_timestamp(exchanged_at),
            self.is_sandbox,
            reason,
            has_new_refresh_token)

        return {
            'access_token': access_token,
            'instance_url': instance_url,
            'token_version': str(
                payload.get('issued_at')
                or self._format_timestamp(exchanged_at)),
        }

    def _prepare_refresh_token_log(self):
        try:
            self.refresh_token_log_path.parent.mkdir(
                parents=True,
                exist_ok=True)
            descriptor = os.open(
                self.refresh_token_log_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600)
            os.close(descriptor)
            os.chmod(self.refresh_token_log_path, 0o600)
        except OSError as exc:
            raise LocalOAuthError(
                'Unable to prepare local OAuth refresh-token log') from exc

    def _append_refresh_token(self, refresh_token, exchanged_at):
        entry = {
            'timestamp': self._format_timestamp(exchanged_at),
            'refresh_token': refresh_token,
        }
        try:
            descriptor = os.open(
                self.refresh_token_log_path,
                os.O_APPEND | os.O_WRONLY)
            with os.fdopen(descriptor, 'a', encoding='utf-8') as log_file:
                log_file.write(json.dumps(entry, separators=(',', ':')))
                log_file.write('\n')
        except OSError as exc:
            raise LocalOAuthError(
                'Unable to append rotated local OAuth refresh token') from exc

    @staticmethod
    def _format_timestamp(value):
        return value.isoformat().replace('+00:00', 'Z')
