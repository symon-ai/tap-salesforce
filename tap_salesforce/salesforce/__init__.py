import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
import backoff
import requests
from requests.exceptions import RequestException
import singer
import singer.utils as singer_utils
from singer import metadata, metrics

from tap_salesforce.salesforce.bulk import Bulk
from tap_salesforce.salesforce.rest import Rest
from tap_salesforce.salesforce.report_rest import ReportRest
from tap_salesforce.salesforce.local_oauth import (
    LocalOAuthClient,
    LocalOAuthError)
from tap_salesforce.salesforce.token_broker import (
    TokenBrokerError,
    fetch_broker_credentials)
from tap_salesforce.salesforce.exceptions import (
    SymonException,
    TapSalesforceException)

LOGGER = singer.get_logger()

BULK_API_TYPE = "BULK"
REST_API_TYPE = "REST"
BROKER_REFRESH_CHECK_AFTER_SECONDS = 900

STRING_TYPES = set([
    'id',
    'string',
    'picklist',
    'textarea',
    'phone',
    'url',
    'reference',
    'multipicklist',
    'combobox',
    'encryptedstring',
    'email',
    'complexvalue',  # TODO: Unverified
    'masterrecord',
    'datacategorygroupreference'
])

NUMBER_TYPES = set([
    'double'
])

NUMBER_OR_STRING_TYPES = set([
    'currency',  # currency types could include the currency iso code if the salesforce org has multicurrency enabled
    # 'percent'  # For some objects/reports, percentages include the actual '%' character, requiring the whole value to be treated as a string. For others, it's just the number
])

DATE_TYPES = set([
    'datetime',
    'date'
])

BINARY_TYPES = set([
    'base64',
    'byte'
])

LOOSE_TYPES = set([
    'anyType',

    # A calculated field's type can be any of the supported
    # formula data types (see https://developer.salesforce.com/docs/#i1435527)
    'calculated'
])


# The following objects are not supported by the bulk API.
UNSUPPORTED_BULK_API_SALESFORCE_OBJECTS = set(['AssetTokenEvent',
                                               'AttachedContentNote',
                                               'EventWhoRelation',
                                               'QuoteTemplateRichTextData',
                                               'TaskWhoRelation',
                                               'SolutionStatus',
                                               'ContractStatus',
                                               'RecentlyViewed',
                                               'DeclinedEventRelation',
                                               'AcceptedEventRelation',
                                               'TaskStatus',
                                               'PartnerRole',
                                               'TaskPriority',
                                               'CaseStatus',
                                               'UndecidedEventRelation',
                                               'OrderStatus'])

# The following objects have certain WHERE clause restrictions so we exclude them.
QUERY_RESTRICTED_SALESFORCE_OBJECTS = set(['Announcement',
                                           'ContentDocumentLink',
                                           'CollaborationGroupRecord',
                                           'Vote',
                                           'IdeaComment',
                                           'FieldDefinition',
                                           'PlatformAction',
                                           'UserEntityAccess',
                                           'RelationshipInfo',
                                           'ContentFolderMember',
                                           'ContentFolderItem',
                                           'SearchLayout',
                                           'SiteDetail',
                                           'EntityParticle',
                                           'OwnerChangeOptionInfo',
                                           'DataStatistics',
                                           'UserFieldAccess',
                                           'PicklistValueInfo',
                                           'RelationshipDomain',
                                           'FlexQueueItem',
                                           'NetworkUserHistoryRecent',
                                           'FieldHistoryArchive',
                                           'RecordActionHistory',
                                           'FlowVersionView',
                                           'FlowVariableView',
                                           'AppTabMember',
                                           'ColorDefinition',
                                           'IconDefinition'])

# The following objects are not supported by the query method being used.
QUERY_INCOMPATIBLE_SALESFORCE_OBJECTS = set(['DataType',
                                             'ListViewChartInstance',
                                             'FeedLike',
                                             'OutgoingEmail',
                                             'OutgoingEmailRelation',
                                             'FeedSignal',
                                             'ActivityHistory',
                                             'EmailStatus',
                                             'UserRecordAccess',
                                             'Name',
                                             'AggregateResult',
                                             'OpenActivity',
                                             'ProcessInstanceHistory',
                                             'OwnedContentDocument',
                                             'FolderedContentDocument',
                                             'FeedTrackedChange',
                                             'CombinedAttachment',
                                             'AttachedContentDocument',
                                             'ContentBody',
                                             'NoteAndAttachment',
                                             'LookedUpFromActivity',
                                             'AttachedContentNote',
                                             'QuoteTemplateRichTextData'])


def log_backoff_attempt(details):
    LOGGER.info(
        "ConnectionError detected, triggering backoff: %d try", details.get("tries"))


def field_to_property_schema(field, mdata, source_type, is_report=False):  # pylint:disable=too-many-branches
    property_schema = {}

    if source_type == 'report':
        field_name = field['label']
        sf_type = field['dataType']
    elif source_type == 'object':
        field_name = field['name']
        sf_type = field['type']

    if sf_type in STRING_TYPES:
        property_schema['type'] = "string"
    elif sf_type in DATE_TYPES:
        property_schema["format"] = "date-time"
        property_schema['type'] = ["string", "null"]
    elif sf_type == "boolean":
        property_schema['type'] = "boolean"
    elif sf_type in NUMBER_OR_STRING_TYPES:
        property_schema['type'] = ["number", "string", "null"]
    elif sf_type in NUMBER_TYPES:
        property_schema['type'] = "number"
    elif sf_type == "percent":
        # percent type field in SF Object returns numeric value without %, but SF Report returns numeric value with %
        property_schema['type'] = "string" if is_report else "number"
    elif sf_type == "address":
        property_schema['type'] = "object"
        property_schema['properties'] = {
            "street": {"type": ["null", "string"]},
            "state": {"type": ["null", "string"]},
            "postalCode": {"type": ["null", "string"]},
            "city": {"type": ["null", "string"]},
            "country": {"type": ["null", "string"]},
            "longitude": {"type": ["null", "number"]},
            "latitude": {"type": ["null", "number"]},
            "geocodeAccuracy": {"type": ["null", "string"]}
        }
    elif sf_type in ("int", "long"):
        property_schema['type'] = "integer"
    elif sf_type == "time":
        property_schema['type'] = "string"
    elif sf_type in LOOSE_TYPES:
        return property_schema, mdata  # No type = all types
    elif sf_type in BINARY_TYPES:
        mdata = metadata.write(
            mdata, ('properties', field_name), "inclusion", "unsupported")
        mdata = metadata.write(mdata, ('properties', field_name),
                               "unsupported-description", "binary data")
        return property_schema, mdata
    elif sf_type == 'location':
        # geo coordinates are numbers or objects divided into two fields for lat/long
        property_schema['type'] = ["number", "object", "null"]
        property_schema['properties'] = {
            "longitude": {"type": ["null", "number"]},
            "latitude": {"type": ["null", "number"]}
        }
    elif sf_type == 'json':
        property_schema['type'] = "string"
    else:
        raise TapSalesforceException(
            "Found unsupported type: {}".format(sf_type))

    # The nillable field cannot be trusted
    if field_name != 'Id' and sf_type != 'location' and sf_type not in DATE_TYPES and sf_type not in NUMBER_OR_STRING_TYPES:
        property_schema['type'] = ["null", property_schema['type']]

    return property_schema, mdata


class Salesforce():
    # pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(self,
                 token=None,
                 quota_percent_per_run=None,
                 quota_percent_total=None,
                 select_fields_by_default=None,
                 default_start_date=None,
                 api_type=None,
                 source_type=None,
                 object_name=None,
                 report_id=None,
                 filters=None,
                 token_broker=None,
                 auth_mode=None,
                 local_oauth=None):
        self.api_type = api_type.upper() if api_type else None
        self.token = token
        self.token_broker = token_broker or {}
        self.auth_mode = auth_mode or 'broker'
        self.session = requests.Session()
        self.local_oauth_client = (
            LocalOAuthClient(local_oauth, session=self.session)
            if self.auth_mode == 'local'
            else None)
        self.access_token = None
        self.instance_url = None
        self.token_version = None
        self.refresh_check_after_seconds = BROKER_REFRESH_CHECK_AFTER_SECONDS
        self._last_broker_check_at = None
        if isinstance(quota_percent_per_run, str) and quota_percent_per_run.strip() == '':
            quota_percent_per_run = None
        if isinstance(quota_percent_total, str) and quota_percent_total.strip() == '':
            quota_percent_total = None
        self.quota_percent_per_run = float(
            quota_percent_per_run) if quota_percent_per_run is not None else 25
        self.quota_percent_total = float(
            quota_percent_total) if quota_percent_total is not None else 80
        self.select_fields_by_default = select_fields_by_default is True or (isinstance(
            select_fields_by_default, str) and select_fields_by_default.lower() == 'true')
        self.default_start_date = default_start_date
        self.rest_requests_attempted = 0
        self.jobs_completed = 0
        self.data_url = "{}/services/data/v52.0/{}"
        self.pk_chunking = False

        self.source_type = source_type if source_type else None
        self.object_name = object_name if object_name else None
        self.report_id = report_id if report_id else None
        self.filters = filters if filters else None

        # validate start_date
        singer_utils.strptime(default_start_date)

        # Validate params
        if source_type != 'object' and source_type != 'report':
            LOGGER.error(
                'Invalid report_type, supported types are report & object')
            raise Exception(
                'Invalid report_type, supported types are report & object')
        if source_type == 'object' and object_name == None:
            LOGGER.error('Object name is required when source type is object')
            raise Exception(
                'Object name is required when source type is object')
        if source_type == 'report' and (report_id == None):
            LOGGER.error(
                'Report id is required when source type is report')
            raise Exception(
                'Report id is required when source type is report')

    def _set_session_credentials(self, access_token, instance_url, token_version=None):
        self.access_token = access_token
        self.instance_url = instance_url
        if token_version is not None:
            self.token_version = token_version

    def _get_known_token_version(self):
        return self.token_version

    def _with_refreshed_auth_header(self, headers):
        refreshed_headers = dict(headers or {})
        refreshed_headers['Authorization'] = "Bearer {}".format(self.access_token)
        refreshed_headers['X-SFDC-Session'] = self.access_token
        return refreshed_headers

    def _validate_broker_token_if_due(self):
        if (self.refresh_check_after_seconds is None
                or self._last_broker_check_at is None):
            return False

        now = time.monotonic()
        if now - self._last_broker_check_at < self.refresh_check_after_seconds:
            return False

        try:
            return self._refresh_auth(reason='periodic')
        except Exception as exc:  # pylint: disable=broad-except
            # A best-effort validation must not fail a read while the current
            # Salesforce token may still be valid. Reactive recovery remains
            # responsible for an actual invalid-session response.
            self._last_broker_check_at = time.monotonic()
            LOGGER.warning(
                "Periodic authentication refresh failed; continuing with "
                "the current Salesforce token: %s",
                exc)
            return False

    @staticmethod
    def _is_invalid_session_error(exc):
        response = getattr(exc, 'response', None)
        if response is None:
            return False

        if getattr(response, 'status_code', None) == 401:
            return True

        try:
            payload = response.json()
        except ValueError:
            response_text = getattr(response, 'text', '') or ''
            return (
                'INVALID_SESSION_ID' in response_text
                or 'InvalidSessionId' in response_text
            )

        def is_invalid_session_code(value):
            normalized = str(value).replace('_', '').upper()
            return normalized == 'INVALIDSESSIONID'

        if isinstance(payload, list):
            return any(
                isinstance(item, dict)
                and is_invalid_session_code(
                    item.get('errorCode', item.get('exceptionCode')))
                for item in payload)

        if isinstance(payload, dict):
            if is_invalid_session_code(
                    payload.get('errorCode', payload.get('exceptionCode'))):
                return True
            for value in payload.values():
                if isinstance(value, list) and any(
                        isinstance(item, dict)
                        and is_invalid_session_code(
                            item.get('errorCode', item.get('exceptionCode')))
                        for item in value):
                    return True

        return False

    def _get_standard_headers(self):
        return {"Authorization": "Bearer {}".format(self.access_token)}

    def _with_current_instance_url(self, url):
        request_url = urlsplit(url)
        instance_url = urlsplit(self.instance_url)
        return urlunsplit((
            instance_url.scheme,
            instance_url.netloc,
            request_url.path,
            request_url.query,
            request_url.fragment,
        ))

    def _get_report_query_headers(self):
        return {"Authorization": "Bearer {}".format(self.access_token),
                "Content-Type": "application/json"}

    # pylint: disable=anomalous-backslash-in-string,line-too-long
    def check_rest_quota_usage(self, headers):
        match = re.search(r'^api-usage=(\d+)/(\d+)$',
                          headers.get('Sforce-Limit-Info'))

        if match is None:
            return

        remaining, allotted = map(int, match.groups())

        LOGGER.info("Used %s of %s daily REST API quota", remaining, allotted)

        percent_used_from_total = (remaining / allotted) * 100
        max_requests_for_run = int(
            (self.quota_percent_per_run * allotted) / 100)

        if percent_used_from_total > self.quota_percent_total:
            total_message = ("Salesforce has reported {}/{} ({:3.2f}%) total REST quota " +
                             "used across all Salesforce Applications. Terminating " +
                             "replication to not continue past the configured percentage " +
                             "of {}% total quota.").format(remaining,
                                                           allotted,
                                                           percent_used_from_total,
                                                           self.quota_percent_total)
            raise SymonException(total_message, 'salesforce.SalesforceApiError') 
        elif self.rest_requests_attempted > max_requests_for_run:
            partial_message = ("This replication job has made {} REST requests ({:3.2f}% of " +
                               "total quota). Terminating replication due to allotted " +
                               "quota of {}% per replication.").format(self.rest_requests_attempted,
                                                                       (self.rest_requests_attempted /
                                                                        allotted) * 100,
                                                                       self.quota_percent_per_run)
            raise SymonException(partial_message, 'salesforce.SalesforceApiError') 

    # pylint: disable=too-many-arguments
    @backoff.on_exception(backoff.expo,
                          (requests.exceptions.ConnectionError,
                           requests.exceptions.Timeout),
                          max_tries=10,
                          factor=2,
                          on_backoff=log_backoff_attempt)
    def _make_request(self,
                      http_method,
                      url,
                      headers=None,
                      body=None,
                      stream=False,
                      params=None,
                      log_body=True,
                      invalid_session_retried=False):
        request_timeout = 5 * 60  # 5 minute request timeout
        if self._validate_broker_token_if_due():
            refreshed_headers = self._with_refreshed_auth_header(headers)
            if headers is not None:
                headers.update(refreshed_headers)
            headers = refreshed_headers
            url = self._with_current_instance_url(url)

        try:
            if http_method == "GET":
                LOGGER.info("Making %s request to %s with params: %s",
                            http_method, url, params)
                resp = self.session.get(url,
                                        headers=headers,
                                        stream=stream,
                                        params=params,
                                        timeout=request_timeout,)
            elif http_method == "POST":
                if log_body:
                    LOGGER.info("Making %s request to %s with body %s",
                                http_method, url, body)
                else:
                    LOGGER.info("Making %s request to %s", http_method, url)
                resp = self.session.post(url,
                                         headers=headers,
                                         data=body,
                                         timeout=request_timeout,)
            else:
                raise TapSalesforceException("Unsupported HTTP method")
        except requests.exceptions.ConnectionError as connection_err:
            LOGGER.error(
                'Took longer than %s seconds to connect to the server', request_timeout)
            raise connection_err
        except requests.exceptions.Timeout as timeout_err:
            LOGGER.error(
                'Took longer than %s seconds to hear from the server', request_timeout)
            raise timeout_err

        try:
            resp.raise_for_status()
        except RequestException as ex:
            if (not invalid_session_retried
                    and self._is_invalid_session_error(ex)):
                self._refresh_auth(reason='invalid_session')
                refreshed_headers = self._with_refreshed_auth_header(headers)
                if headers is not None:
                    headers.update(refreshed_headers)
                return self._make_request(
                    http_method,
                    self._with_current_instance_url(url),
                    headers=refreshed_headers,
                    body=body,
                    stream=stream,
                    params=params,
                    log_body=log_body,
                    invalid_session_retried=True)
            raise ex
        if resp.headers.get('Sforce-Limit-Info') is not None:
            self.rest_requests_attempted += 1
            self.check_rest_quota_usage(resp.headers)
        return resp

    def login(self):
        self._refresh_auth(reason='startup')

    def _refresh_auth(self, reason):
        if self.auth_mode == 'local':
            return self._login_local(reason)
        return self._login_broker(reason)

    def _login_broker(self, reason='startup'):
        LOGGER.info("Attempting login via token broker (%s)", reason)
        try:
            previous_credentials = (
                self.access_token,
                self.instance_url,
                self.token_version,
            )
            credentials = fetch_broker_credentials(
                endpoint=self.token_broker['endpoint'],
                reason=reason,
                task_auth_token=self.token_broker['task_auth_token'],
                known_token_version=self._get_known_token_version(),
                session=self.session)
            self._set_session_credentials(
                credentials['access_token'],
                credentials['instance_url'],
                credentials['token_version'])
            self._last_broker_check_at = time.monotonic()
            LOGGER.info("Token broker login successful")
            return previous_credentials != (
                self.access_token,
                self.instance_url,
                self.token_version,
            )
        except TokenBrokerError as exc:
            raise Exception(str(exc)) from exc

    def _login_local(self, reason='startup'):
        LOGGER.info("Attempting login via local OAuth (%s)", reason)
        try:
            previous_credentials = (
                self.access_token,
                self.instance_url,
                self.token_version,
            )
            credentials = self.local_oauth_client.fetch_credentials(reason)
            self._set_session_credentials(
                credentials['access_token'],
                credentials['instance_url'],
                credentials['token_version'])
            self._last_broker_check_at = time.monotonic()
            LOGGER.info("Local OAuth login successful")
            return previous_credentials != (
                self.access_token,
                self.instance_url,
                self.token_version,
            )
        except LocalOAuthError as exc:
            raise Exception(str(exc)) from exc

    def describe(self):
        """Describes a specific object or a specific report"""
        headers = self._get_standard_headers()

        if self.source_type == 'object':
            endpoint = f'sobjects/{self.object_name}/describe'
            endpoint_tag = self.object_name
            url = self.data_url.format(self.instance_url, endpoint)
        elif self.source_type == 'report':
            endpoint = f'analytics/reports/{self.report_id}/describe'
            endpoint_tag = self.report_id
            url = self.data_url.format(self.instance_url, endpoint)

        with metrics.http_request_timer("describe") as timer:
            timer.tags['endpoint'] = endpoint_tag
            resp = self._make_request('GET', url, headers=headers)

        return resp.json()

    # pylint: disable=no-self-use
    def _get_selected_properties(self, catalog_entry):
        mdata = metadata.to_map(catalog_entry['metadata'])
        properties = catalog_entry['schema'].get('properties', {})

        return [k for k in properties.keys()
                if singer.should_sync_field(metadata.get(mdata, ('properties', k), 'inclusion'),
                                            metadata.get(
                                                mdata, ('properties', k), 'selected'),
                                            self.select_fields_by_default)]

    def get_start_date(self, state, catalog_entry):
        catalog_metadata = metadata.to_map(catalog_entry['metadata'])
        replication_key = catalog_metadata.get((), {}).get('replication-key')

        return (singer.get_bookmark(state,
                                    catalog_entry['tap_stream_id'],
                                    replication_key) or self.default_start_date)

    def _build_query_string(self, catalog_entry, start_date, end_date=None, order_by_clause=True):
        selected_properties = self._get_selected_properties(catalog_entry)

        query = "SELECT {} FROM {}".format(
            ",".join(selected_properties), catalog_entry['stream'])

        where_clauses = []

        if 'IsDeleted' in selected_properties:
            where_clauses.append("IsDeleted = false")

        catalog_metadata = metadata.to_map(catalog_entry['metadata'])
        replication_key = catalog_metadata.get((), {}).get('replication-key')

        if (self.filters):
            source_column_types = catalog_entry.get('source_column_types', {})
            extra = self.filter_sql(self.filters, source_column_types)
            where_clauses.append(extra)

        if replication_key:
            where_clauses.append("{} >= {} ".format(
                replication_key,
                start_date))
            if end_date:
                where_clauses.append("{} < {}".format(
                    replication_key, end_date))

        if len(where_clauses) > 0:
            where_clause = ' AND '.join(where_clauses)
            query += ' WHERE '
            query += where_clause

        if replication_key and order_by_clause:
            order_by = " ORDER BY {} ASC".format(replication_key)
            query += order_by

        return query

    def query(self, catalog_entry, state):
        if self.api_type == BULK_API_TYPE:
            bulk = Bulk(self)
            return bulk.query(catalog_entry, state)
        elif self.api_type == REST_API_TYPE:
            rest = Rest(self)
            return rest.query(catalog_entry, state)
        else:
            raise TapSalesforceException(
                "api_type should be REST or BULK was: {}".format(
                    self.api_type))

    def query_report(self, catalog_entry, state):
        reportRest = ReportRest(self)
        return reportRest.query(catalog_entry, state)

    def get_blacklisted_objects(self):
        if self.api_type == BULK_API_TYPE:
            return UNSUPPORTED_BULK_API_SALESFORCE_OBJECTS.union(
                QUERY_RESTRICTED_SALESFORCE_OBJECTS).union(QUERY_INCOMPATIBLE_SALESFORCE_OBJECTS)
        elif self.api_type == REST_API_TYPE:
            return QUERY_RESTRICTED_SALESFORCE_OBJECTS.union(QUERY_INCOMPATIBLE_SALESFORCE_OBJECTS)
        else:
            raise TapSalesforceException(
                "api_type should be REST or BULK was: {}".format(
                    self.api_type))

    # pylint: disable=line-too-long
    def get_blacklisted_fields(self):
        if self.api_type == BULK_API_TYPE:
            return {('EntityDefinition', 'RecordTypesSupported'): "this field is unsupported by the Bulk API."}
        elif self.api_type == REST_API_TYPE:
            return {}
        else:
            raise TapSalesforceException(
                "api_type should be REST or BULK was: {}".format(
                    self.api_type))

    def filter_sql(self, filter, source_col_types):
        if (filter["filterType"] == "statement"):
            return self.filter_statement_sql(filter, source_col_types)
        else:
            filters = filter["filters"]

            group_filters_sql = []
            for f in filters:
                child_sql = self.filter_sql(f, source_col_types)
                if child_sql is not None:
                    group_filters_sql.append(child_sql)

            if len(group_filters_sql) > 0:
                group_op_sql = " {} ".format(filter["op"])
                return "({})".format(group_op_sql.join(group_filters_sql))
            else:
                return None

    def filter_statement_sql(self, statement, source_col_types):
        lhs_sql = self.filter_operand_sql(statement["lhs"])
        op_sql = self.filter_op_sql(statement["op"])

        if "rhs" in statement:
            rhs_sql = self.filter_operand_sql(statement["rhs"])

            if statement["rhs"].get("litType") == 'date':
                # Salesforce has date and datetime columns. Only datetime columns can include time, or query will throw an error
                if source_col_types.get(lhs_sql) == 'datetime':
                    # salesforce only store date in utc and needs Z at the end instead of +00:00, our filters need to match that
                    rhs_sql = f"{datetime.fromisoformat(rhs_sql).replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')}"
                return f"({lhs_sql} {op_sql} {rhs_sql})"
            if statement["rhs"].get("litType") == 'number':
                if source_col_types.get(lhs_sql) in ('int', 'long'):
                    # float first in case string value contains decimals
                    rhs_sql = int(float(rhs_sql))
                return f"({lhs_sql} {op_sql} {rhs_sql})"
            if statement["rhs"].get("litType") == 'boolean':
                return f"({lhs_sql} {op_sql} {rhs_sql})"
            if statement["op"] == "starts_with":
                return f"({lhs_sql} {op_sql} '{rhs_sql}%')"
            if statement["op"] == "ends_with":
                return f"({lhs_sql} {op_sql} '%{rhs_sql}')"
            if statement["op"] == "contains" or statement["op"] == "not_contains":
                return f"({lhs_sql} {op_sql} '%{rhs_sql}%')"

            return f"({lhs_sql} {op_sql} '{rhs_sql}')"
        else:
            return f"({lhs_sql} {op_sql})"

    def filter_operand_sql(self, operand):
        if operand["operandType"] == "column":
            return operand['name']
        else:
            return operand['value']

    def filter_op_sql(self, op):
        ops = {
            "less_than": "<",
            "less_than_equals": "<=",
            "equals": "=",
            "not_equals": "!=",
            "greater_than_equals": ">=",
            "greater_than": ">",
            "is_null": "= null",
            "is_not_null": "!= null",
            "starts_with": "LIKE",
            "ends_with": "LIKE",
            "contains": "LIKE",
            "not_contains": "NOT LIKE"
        }

        return ops[op]

    def sql_esc_cname(self, cname):
        return f"'{cname}'" if "'" in cname else cname