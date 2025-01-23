import re
import threading
from datetime import datetime, timezone
import backoff
import requests
from requests.exceptions import RequestException
import singer
import singer.utils as singer_utils
from singer import metadata, metrics

from tap_salesforce.salesforce.bulk import Bulk
from tap_salesforce.salesforce.rest import Rest
from tap_salesforce.salesforce.report_rest import ReportRest
from tap_salesforce.salesforce.exceptions import (
    SymonException,
    TapSalesforceException)

LOGGER = singer.get_logger()

# The minimum expiration setting for SF Refresh Tokens is 15 minutes
REFRESH_TOKEN_EXPIRATION_PERIOD = 900

BULK_API_TYPE = "BULK"
REST_API_TYPE = "REST"

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
                 refresh_token=None,
                 token=None,
                 sf_client_id=None,
                 sf_client_secret=None,
                 quota_percent_per_run=None,
                 quota_percent_total=None,
                 is_sandbox=None,
                 select_fields_by_default=None,
                 default_start_date=None,
                 api_type=None,
                 source_type=None,
                 object_name=None,
                 report_id=None,
                 filters=None):
        self.api_type = api_type.upper() if api_type else None
        self.refresh_token = refresh_token
        self.token = token
        self.sf_client_id = sf_client_id
        self.sf_client_secret = sf_client_secret
        self.session = requests.Session()
        self.access_token = None
        self.instance_url = None
        if isinstance(quota_percent_per_run, str) and quota_percent_per_run.strip() == '':
            quota_percent_per_run = None
        if isinstance(quota_percent_total, str) and quota_percent_total.strip() == '':
            quota_percent_total = None
        self.quota_percent_per_run = float(
            quota_percent_per_run) if quota_percent_per_run is not None else 25
        self.quota_percent_total = float(
            quota_percent_total) if quota_percent_total is not None else 80
        self.is_sandbox = is_sandbox is True or (isinstance(
            is_sandbox, str) and is_sandbox.lower() == 'true')
        self.select_fields_by_default = select_fields_by_default is True or (isinstance(
            select_fields_by_default, str) and select_fields_by_default.lower() == 'true')
        self.default_start_date = default_start_date
        self.rest_requests_attempted = 0
        self.jobs_completed = 0
        self.login_timer = None
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

    def _get_standard_headers(self):
        return {"Authorization": "Bearer {}".format(self.access_token)}

    def _get_report_query_headers(self):
        return {"Authorization": "Bearer {}".format(self.access_token),
                "Content-Type": "application/json"}

    # pylint: disable=anomalous-backslash-in-string,line-too-long
    def check_rest_quota_usage(self, headers):
        match = re.search('^api-usage=(\d+)/(\d+)$',
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
    def _make_request(self, http_method, url, headers=None, body=None, stream=False, params=None):
        request_timeout = 5 * 60  # 5 minute request timeout
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
                LOGGER.info("Making %s request to %s with body %s",
                            http_method, url, body)
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
            raise ex
        if resp.headers.get('Sforce-Limit-Info') is not None:
            self.rest_requests_attempted += 1
            self.check_rest_quota_usage(resp.headers)
        return resp

    def login(self):
        if self.is_sandbox:
            login_url = 'https://test.salesforce.com/services/oauth2/token'
        else:
            login_url = 'https://login.salesforce.com/services/oauth2/token'

        login_body = {'grant_type': 'refresh_token', 'client_id': self.sf_client_id,
                      'client_secret': self.sf_client_secret, 'refresh_token': self.refresh_token}

        LOGGER.info("Attempting login via OAuth2")

        resp = None
        try:
            resp = self._make_request("POST", login_url, body=login_body, headers={
                                      "Content-Type": "application/x-www-form-urlencoded"})

            LOGGER.info("OAuth2 login successful")

            auth = resp.json()

            self.access_token = auth['access_token']
            self.instance_url = auth['instance_url']
        except Exception as e:
            error_message = str(e)
            if resp is None and hasattr(e, 'response') and e.response is not None:  # pylint:disable=no-member
                resp = e.response  # pylint:disable=no-member
            # NB: requests.models.Response is always falsy here. It is false if status code >= 400
            if isinstance(resp, requests.models.Response):
                error_message = error_message + \
                    ", Response from Salesforce: {}".format(resp.text)
            raise Exception(error_message) from e
        finally:
            LOGGER.info("Starting new login timer")
            self.login_timer = threading.Timer(
                REFRESH_TOKEN_EXPIRATION_PERIOD, self.login)
            # The timer should be a daemon thread so the process exits.
            self.login_timer.daemon = True
            self.login_timer.start()

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
