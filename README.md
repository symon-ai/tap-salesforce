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
  "client_id": "secret_client_id",
  "client_secret": "secret_client_secret",
  "refresh_token": "abc123",
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
  "client_id": "secret_client_id",
  "client_secret": "secret_client_secret",
  "refresh_token": "abc123",
  "start_date": "2017-11-02T00:00:00Z",
  "api_type": "BULK",
  "select_fields_by_default": true,
  "source_type": "object",
  "object_name": "OBJECT_NAME"
}
```

The `client_id` and `client_secret` keys are your OAuth Salesforce App secrets. The `refresh_token` is a secret created during the OAuth flow. For more info on the Salesforce OAuth flow, visit the [Salesforce documentation](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_understanding_web_server_oauth_flow.htm). Additionnaly, if the Salesforce Sandbox is to be used to run the tap, the parameter `"is_sandbox": true` must be passed to the config.

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
