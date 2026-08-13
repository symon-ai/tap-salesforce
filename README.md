# tap-salesforce

[![PyPI version](https://badge.fury.io/py/tap-mysql.svg)](https://badge.fury.io/py/tap-salesforce)
[![CircleCI Build Status](https://circleci.com/gh/singer-io/tap-salesforce.png)](https://circleci.com/gh/singer-io/tap-salesforce.png)

[Singer](https://www.singer.io/) tap that extracts data from a [Salesforce](https://www.salesforce.com/) database and produces JSON-formatted data following the [Singer spec](https://github.com/singer-io/getting-started/blob/master/docs/SPEC.md#singer-specification).

# Install and Run

Ensure poetry is installed on your machine. 

- This command will return the installed version of poetry if it is installed.
```
poetry --version
```

- If not, install poetry using the following commands (from https://python-poetry.org/docs/#installation):
```
curl -sSL https://install.python-poetry.org | python3 -
PATH=~/.local/bin:$PATH
```

Within the `tap-salesforce` directory, install dependencies:
```
poetry install
```

Then run the tap:
```
poetry run tap-salesforce <options>
```

## Symon Specific updates

Originally taps --discover takes a long time since it gets the schema of objects one by one, also it doesn't support getting reports.

- Added a new config to only discover one report or one object
- Added the ability to process reports

- When importing an object, we ignore rows that have been deleted (and are in the recycling bin on Salesforce)

# Quickstart

## Install the tap

```
> pip install tap-salesforce
```

## Create a Config file

Config for reading a report

```
{
  "token_broker": {
    "endpoint": "https://api.example.com/connections/oauth/CONNECTION_ID/access-token",
    "connection_id": "CONNECTION_ID",
    "task_auth_token": "TASK_AUTH_TOKEN"
  },
  "start_date": "2017-11-02T00:00:00Z",
  "api_type": "BULK",
  "select_fields_by_default": true,
  "source_type": "report",
  "report_id": "REPORT_ID"
}
```

Config for reading an object

```
{
  "token_broker": {
    "endpoint": "https://api.example.com/connections/oauth/CONNECTION_ID/access-token",
    "connection_id": "CONNECTION_ID",
    "task_auth_token": "TASK_AUTH_TOKEN"
  },
  "start_date": "2017-11-02T00:00:00Z",
  "api_type": "BULK",
  "select_fields_by_default": true,
  "source_type": "object",
  "object_name": "OBJECT_NAME"
}
```

Production imports use `token_broker`. Its endpoint supplies Salesforce access
tokens authorized by the short-lived TaskAuth token. If `token_broker` is
present, it always takes precedence over local OAuth configuration.

## Running locally

### Recommended: token broker

The token broker is the recommended local-testing method. It avoids copying
Salesforce Connected App credentials and refresh tokens to the workstation,
and the stage connection service handles refresh-token rotation.

You need:

- The non-production stage and AWS account.
- The organization ID that owns the connection.
- The Salesforce connection ID. If only its name is known, resolve that name
  to exactly one connection in the selected stage first.
- The stage API router URL and its currently active blue/green API URL.
- A short-lived TaskAuth token for the organization.

To prepare broker configuration:

1. Confirm the target stage, organization ID, and connection ID.
2. Check `/blue-green-deploy/current-release` on the stage API router and note
   whether `b` or `g` is active.
3. From `app/utils/typescript`, generate a 24-hour TaskAuth token with
   `generateTaskAuthToken`. Its `--apiUrl` must be the active blue/green host,
   and the AWS credentials must belong to the stage's account.
4. Set `token_broker.endpoint` to the stage connection access-token route,
   ending in `/connections/oauth/<CONNECTION_ID>/access-token`. The normal
   stage router URL can be used for this broker endpoint.
5. Set `token_broker.connection_id` and `token_broker.task_auth_token`.
6. Add the normal tap settings: `start_date`, `api_type`,
   `select_fields_by_default`, `source_type`, and either `object_name` or
   `report_id`.
7. Keep the config outside source control with owner-only permissions, then
   run discovery and sync normally.

### Alternative: direct local OAuth

Use direct local OAuth only when depending on a stage's connection API at
runtime is not desired. It still requires authorized, one-time retrieval of
the selected connection's credentials. This mode is for development only and
must never be used in an import activity container.

Before preparing the config:

1. Identify the non-production stage and a unique Salesforce connection ID or
   name in that stage.
2. Resolve the connection record and note its connection ID and sandbox flag.
3. Using approved stage tooling, obtain the Salesforce Connected App
   `client_id` and `client_secret`, and decrypt the connection's current
   `refresh_token`. Never print these values or place them in shell history.
4. Confirm the Connected App's network policy permits token exchanges from
   the workstation. Otherwise Salesforce may return
   `invalid_grant: ip restricted by app developer`.
5. Create a config outside the repository, restrict it to owner read/write,
   omit `token_broker`, and add:

The stored Salesforce access token and instance URL are not required in
`local_oauth`; the refresh-token exchange returns fresh values.

```json
{
  "auth_mode": "local",
  "local_oauth": {
    "client_id": "CONNECTED_APP_CLIENT_ID",
    "client_secret": "CONNECTED_APP_CLIENT_SECRET",
    "refresh_token": "CURRENT_REFRESH_TOKEN",
    "is_sandbox": false,
    "refresh_token_log_path": ".salesforce-oauth/refresh-tokens.jsonl"
  },
  "start_date": "2017-11-02T00:00:00Z",
  "api_type": "BULK",
  "select_fields_by_default": true,
  "source_type": "object",
  "object_name": "OBJECT_NAME"
}
```

#### Where the local credentials come from

For Salesforce connections, the Connected App client secret is not stored in
the connection DynamoDB item:

- `client_id` normally comes from `/<stage>/integration/client-ids`, the
  stage's aggregated connection-client-ID parameter, under the
  `salesforceClientID` key.
- `client_secret` normally comes from a stage-specific SSM `SecureString`
  parameter such as `/<stage>/integration/secret/salesforce`. Verify the
  deployed parameter name, read it with SSM decryption enabled, and pass it
  directly into the protected config; do not print it.
- `refresh_token` is stored as AWS Encryption SDK ciphertext in
  `ConnectionModel.credentials.refreshToken` in the stage's Connections V2
  DynamoDB item. The access token in the same credentials object is encrypted
  as well, but local OAuth does not need it.

Use the same stage account, AWS region, and KMS key as the connection service.
Resolve the KMS key from the deployed aws-common stack's `MainKeyArn` output;
do not hard-code a key from another stage. The application encryption format
is AWS Encryption SDK ciphertext encoded as base64, not a raw `kms:Decrypt`
blob. Use the shared `EncryptionHelper`:

```typescript
import { EncryptionHelper } from '@symon-ai/wisepipe-aws-common';

const encryption = new EncryptionHelper(mainKeyArn);
const refreshToken = await encryption.decrypt(encryptedRefreshToken);
```

For encryption, the connection service uses this exact encryption context:

```typescript
const encryptedRefreshToken = await encryption.encrypt(
  refreshToken,
  stage,
  'connection',
  region
);
```

The encryption context values must match the connection's stage and region.
Run this in a process with the correct stage AWS profile and KMS permissions.
Write decrypted values directly to an owner-only config outside source
control; never send plaintext credentials to stdout, logs, shell arguments, or
command history.

`is_sandbox` defaults to `false`; set it to `true` to use
`test.salesforce.com`. The refresh-token log path is optional and defaults to
the gitignored path shown above. Setting it to an empty string is invalid. The
log file is owner-readable/writable only.

When running direct local OAuth:

1. Run discovery or sync with the protected config.
2. Check the local-only authentication log fields
   `new_refresh_token_returned`, `refresh_token_logged`, and
   `refresh_token_log_path`. Token values are never written to application
   logs.
3. Salesforce may rotate the refresh token during every process startup,
   periodic refresh, or invalid-session recovery. The tap immediately uses
   the newest token in memory and appends it to the JSONL recovery file with
   an ISO date-time.
4. Before starting a separate tap process, replace the config's
   `local_oauth.refresh_token` with the latest JSONL entry. Under Refresh Token
   Rotation, the previous value may already be invalid.

### Repair the stage connection after direct local OAuth

If the starting token came from a stage connection, that connection may be
broken until it receives the latest rotated token:

1. Stop all local runs so no newer token can be produced.
2. Select the last JSONL entry by timestamp and confirm it is the latest token.
3. Read the exact Connections V2 item again and preserve its binary `UserID`
   and `ConnectionID` keys, current encrypted refresh-token value,
   and `OAuthTokenVersion`. Do not derive or guess the binary keys from the
   displayed connection name.
4. Encrypt the latest JSONL refresh token with `EncryptionHelper.encrypt`,
   using the same `MainKeyArn` and `{ stage, purpose: "connection", origin:
   region }` context described above.
5. Perform a conditional DynamoDB update that:
   - replaces only `ConnectionModel.credentials.refreshToken`;
   - sets `RefreshTokenRotatedAt` to the current epoch time in milliseconds;
   - removes stale `OAuthTokenValidatedAt` and `RefreshTokenLastUsedAt`;
   - requires the item to exist and the stored encrypted refresh token to
     still equal the value read in step 3.
6. Do not overwrite the full `ConnectionModel`, stored access token, connection
   metadata, `Version`, or `OAuthTokenVersion`. The access-token version must
   continue to describe the stored access token; the broker will replace both
   tokens and advance its metadata on its next required refresh.
7. Read the item back without decrypting or printing secrets and confirm the
   ciphertext changed.

The `start_date` is used by the tap as a bound on SOQL queries when searching for records. This should be an [RFC3339](https://www.ietf.org/rfc/rfc3339.txt) formatted date-time, like "2018-01-08T00:00:00Z". For more details, see the [Singer best practices for dates](https://github.com/singer-io/getting-started/blob/master/BEST_PRACTICES.md#dates).

The `api_type` is used to switch the behavior of the tap between using Salesforce's "REST" and "BULK" APIs. When new fields are discovered in Salesforce objects, the `select_fields_by_default` key describes whether or not the tap will select those fields by default.

## Run Discovery

To run discovery mode, execute the tap with the config file.

```
> tap-salesforce --config config.json --discover > properties.json
```

## Sync Data

To sync data, select fields in the `properties.json` output and run the tap.

```
> tap-salesforce --config config.json --properties properties.json [--state state.json]
```

## Package manager

We only use poetry to manage our packages. Pipfile is there because our code scan doesn't support poetry.lock. So we do the following hack to generate Pipfile and Pipfile.lock based on our poetry.lock:
# 1. Export all dependencies from poetry.lock to requirements.txt
```
poetry export -f requirements.txt --output requirements.txt --without-hashes
```
# 1b. (Optional) Make sure pipenv has the right python version
Check:
```
pipenv --support
```
Install:
```
python -m pip install --user pipenv
```

# 2. Generate Pipfile and Pipfile.lock from requirements.txt (make sure you pass in right version of python)
```
pipenv install --python 3.13 -r requirements.txt
```

Check that the required python version in the Pipfile matches your expected python version. For some reason even if requirements.txt specify the right python version pipenv can still default to a different version based on the some stale versioning in the venv. In which case, do the following:

# 1. Delete the Pipfile and lock, and deactivate your venv

# 2. Delete the venv with `pipenv --rm`

# 3. Re-run the pipenv install command

Copyright &copy; 2017 Stitch
