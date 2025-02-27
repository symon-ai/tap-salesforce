import re
import time
import singer
import singer.utils as singer_utils
from singer import Transformer, metadata, metrics
from singer import SingerSyncError
from requests.exceptions import RequestException
from tap_salesforce.salesforce.bulk import Bulk
from tap_salesforce.salesforce.exceptions import SymonException

LOGGER = singer.get_logger()

BLACKLISTED_FIELDS = set(['attributes'])


def remove_blacklisted_fields(data):
    return {k: v for k, v in data.items() if k not in BLACKLISTED_FIELDS}

# pylint: disable=unused-argument


def transform_bulk_data_hook(data, typ, schema):
    result = data
    if isinstance(data, dict):
        result = remove_blacklisted_fields(data)

    # Salesforce can return the value '0.0' for integer typed fields. This
    # causes a schema violation. Convert it to '0' if schema['type'] has
    # integer.
    if data == '0.0' and 'integer' in schema.get('type', []):
        result = '0'

    # Salesforce Bulk API returns CSV's with empty strings for text fields.
    # When the text field is nillable and the data value is an empty string,
    # change the data so that it is None.
    if data == "" and "null" in schema['type']:
        result = None

    return result


def get_stream_version(catalog_entry, state):
    tap_stream_id = catalog_entry['tap_stream_id']
    catalog_metadata = metadata.to_map(catalog_entry['metadata'])
    replication_key = catalog_metadata.get((), {}).get('replication-key')

    if singer.get_bookmark(state, tap_stream_id, 'version') is None:
        stream_version = int(time.time() * 1000)
    else:
        stream_version = singer.get_bookmark(state, tap_stream_id, 'version')

    if replication_key:
        return stream_version
    return int(time.time() * 1000)


def resume_syncing_bulk_query(sf, catalog_entry, job_id, state, counter):
    bulk = Bulk(sf)
    current_bookmark = singer.get_bookmark(
        state, catalog_entry['tap_stream_id'], 'JobHighestBookmarkSeen') or sf.get_start_date(state, catalog_entry)
    current_bookmark = singer_utils.strptime_with_tz(current_bookmark)
    batch_ids = singer.get_bookmark(
        state, catalog_entry['tap_stream_id'], 'BatchIDs')

    start_time = singer_utils.now()
    stream = catalog_entry['stream']
    stream_id = catalog_entry['tap_stream_id']
    stream_alias = catalog_entry.get('stream_alias')
    catalog_metadata = metadata.to_map(catalog_entry.get('metadata'))
    replication_key = catalog_metadata.get((), {}).get('replication-key')
    stream_version = get_stream_version(catalog_entry, state)
    schema = catalog_entry['schema']

    if not bulk.job_exists(job_id):
        LOGGER.info(
            "Found stored Job ID that no longer exists, resetting bookmark and removing JobID from state.")
        return counter

    # Iterate over the remaining batches, removing them once they are synced
    for batch_id in batch_ids[:]:
        with Transformer(pre_hook=transform_bulk_data_hook) as transformer:
            for rec in bulk.get_batch_results(job_id, batch_id, catalog_entry):
                counter.increment()
                rec = transformer.transform(rec, schema)
                rec = fix_record_anytype(rec, schema)
                singer.write_message(
                    singer.RecordMessage(
                        stream=(stream_id or stream_alias or stream),
                        record=rec,
                        version=stream_version,
                        time_extracted=start_time))

                # Update bookmark if necessary
                replication_key_value = replication_key and singer_utils.strptime_with_tz(
                    rec[replication_key])
                if replication_key_value and replication_key_value <= start_time and replication_key_value > current_bookmark:
                    current_bookmark = singer_utils.strptime_with_tz(
                        rec[replication_key])

        state = singer.write_bookmark(state,
                                      catalog_entry['tap_stream_id'],
                                      'JobHighestBookmarkSeen',
                                      singer_utils.strftime(current_bookmark))
        batch_ids.remove(batch_id)
        LOGGER.info(
            "Finished syncing batch %s. Removing batch from state.", batch_id)
        LOGGER.info("Batches to go: %d", len(batch_ids))
        singer.write_state(state)

    return counter


def sync_stream(sf, catalog_entry, state):
    stream = catalog_entry['stream']

    with metrics.record_counter(stream) as counter:
        try:
            if sf.source_type == 'object':
                sync_records(sf, catalog_entry, state, counter)
            elif sf.source_type == 'report':
                sync_report(sf, catalog_entry, state, counter)
            singer.write_state(state)
        except RequestException as ex:
            if ex.response is not None:
                code, message = None, None
                try: 
                    resp_json = ex.response.json()
                    if isinstance(resp_json, list):
                        resp_json = resp_json[0]

                    code = resp_json.get('exceptionCode', None) or resp_json.get('errorCode', None)
                    message = resp_json.get('exceptionMessage', None) or resp_json.get('message', None)
                except Exception:
                    pass

                if code is not None and message is not None:
                    raise SymonException(f'Import failed with the following Salesforce error: (error code: {code}) {message}', 'salesforce.SalesforceApiError')
                raise Exception("{} Response: {}, (Stream: {})".format(
                    ex, ex.response.text, stream)) from ex
            
            raise
        except Exception as ex:
            message = str(ex)
            if any(phrase in message for phrase in (
                "total REST quota used across all Salesforce Applications",
                "Terminating replication due to allotted"
            )):
                raise
            if "OPERATION_TOO_LARGE: exceeded 100000 distinct who/what's" in message:
                raise SingerSyncError("OPERATION_TOO_LARGE: exceeded 100000 distinct who/what's. " +
                                      "Consider asking your Salesforce System Administrator to provide you with the " +
                                      "`View All Data` profile permission. (Stream: {})".format(stream)) from ex
            if "Failed to process query: INVALID_FIELD" in message and "No such column" in message and "on entity" in message:
                column, entity = None, None
                try:
                    # error message in form of: No such column '<column>' on entity '<entity>.'.
                    # for multiple columns, the error message still includes only the first column.
                    core_message = message[message.index(
                        "No such column"):].split(" ")
                    column = core_message[3].replace("'", '"')
                    entity = core_message[6].replace("'", '"')[:-1]
                except:
                    pass
                if column is not None and entity is not None:
                    raise SymonException(
                        f'We can\'t find {column} column on {entity} {sf.source_type}. Review the Field Level Permissions in Salesforce and try importing your data again.', 'salesforce.InvalidField')

            match = re.search(
                "value of filter criterion for field '([A-Za-z0-9_]*)' must be of type ([A-Za-z0-9]*)", message)
            if match is not None:
                # Get filter value from error message
                field_name = match.group(1)
                operand_value_match = re.search(
                    f"\({field_name} .* (.*?)\)", message)
                if operand_value_match is not None:
                    raise SymonException(
                        f"Invalid filter: Field {field_name} filter value of {operand_value_match.group(1)} does not match field type of {match.group(2)}", 'salesforce.InvalidFilter')
                raise SymonException(
                    f"Invalid filter: Value of filter criterion for field '{field_name}' is of invalid type", 'salesforce.InvalidFilter')

            raise Exception("{}, (Stream: {})".format(
                ex, stream)) from ex

        return counter


def sync_records(sf, catalog_entry, state, counter):
    chunked_bookmark = singer_utils.strptime_with_tz(
        sf.get_start_date(state, catalog_entry))
    stream = catalog_entry['stream']
    stream_id = catalog_entry['tap_stream_id']
    schema = catalog_entry['schema']
    stream_alias = catalog_entry.get('stream_alias')
    catalog_metadata = metadata.to_map(catalog_entry['metadata'])
    replication_key = catalog_metadata.get((), {}).get('replication-key')
    stream_version = get_stream_version(catalog_entry, state)
    activate_version_message = singer.ActivateVersionMessage(stream=(stream_alias or stream),
                                                             version=stream_version)

    start_time = singer_utils.now()

    LOGGER.info('Syncing Salesforce data for stream %s', stream)

    for rec in sf.query(catalog_entry, state):
        counter.increment()
        with Transformer(pre_hook=transform_bulk_data_hook) as transformer:
            rec = transformer.transform(rec, schema)
        rec = fix_record_anytype(rec, schema)
        singer.write_message(
            singer.RecordMessage(
                stream=(stream_id or stream_alias or stream),
                record=rec,
                version=stream_version,
                time_extracted=start_time))

        replication_key_value = replication_key and singer_utils.strptime_with_tz(
            rec[replication_key])

        if sf.pk_chunking:
            if replication_key_value and replication_key_value <= start_time and replication_key_value > chunked_bookmark:
                # Replace the highest seen bookmark and save the state in case we need to resume later
                chunked_bookmark = singer_utils.strptime_with_tz(
                    rec[replication_key])
                state = singer.write_bookmark(
                    state,
                    catalog_entry['tap_stream_id'],
                    'JobHighestBookmarkSeen',
                    singer_utils.strftime(chunked_bookmark))
                singer.write_state(state)
        # Before writing a bookmark, make sure Salesforce has not given us a
        # record with one outside our range
        elif replication_key_value and replication_key_value <= start_time:
            state = singer.write_bookmark(
                state,
                catalog_entry['tap_stream_id'],
                replication_key,
                rec[replication_key])
            singer.write_state(state)

        # Tables with no replication_key will send an
        # activate_version message for the next sync
    if not replication_key:
        singer.write_message(activate_version_message)
        state = singer.write_bookmark(
            state, catalog_entry['tap_stream_id'], 'version', None)

    # If pk_chunking is set, only write a bookmark at the end
    if sf.pk_chunking:
        # Write a bookmark with the highest value we've seen
        state = singer.write_bookmark(
            state,
            catalog_entry['tap_stream_id'],
            replication_key,
            singer_utils.strftime(chunked_bookmark))


def sync_report(sf, catalog_entry, state, counter):

    # Make sure that the report id in the config & stream are the same
    if catalog_entry['tap_stream_id'] != sf.report_id:
        LOGGER.error(
            'report_id in the stream should match the report_id in the config')
        raise Exception(
            'report_id in the stream should match the report_id in the config')

    chunked_bookmark = singer_utils.strptime_with_tz(
        sf.get_start_date(state, catalog_entry))
    stream = catalog_entry['stream']
    stream_id = catalog_entry['tap_stream_id']
    schema = catalog_entry['schema']
    stream_alias = catalog_entry.get('stream_alias')
    catalog_metadata = metadata.to_map(catalog_entry['metadata'])
    replication_key = catalog_metadata.get((), {}).get('replication-key')
    stream_version = get_stream_version(catalog_entry, state)
    activate_version_message = singer.ActivateVersionMessage(stream=(stream_alias or stream),
                                                             version=stream_version)

    start_time = singer_utils.now()

    LOGGER.info('Syncing Salesforce report data for stream %s', stream)

    for rec in sf.query_report(catalog_entry, state):
        counter.increment()
        with Transformer() as transformer:
            rec = transformer.transform(rec, schema)
        rec = fix_record_anytype(rec, schema)

        singer.write_message(
            singer.RecordMessage(
                stream=(stream_id or stream_alias or stream),
                record=rec,
                version=stream_version,
                time_extracted=start_time))

        replication_key_value = replication_key and singer_utils.strptime_with_tz(
            rec[replication_key])

        if sf.pk_chunking:
            if replication_key_value and replication_key_value <= start_time and replication_key_value > chunked_bookmark:
                # Replace the highest seen bookmark and save the state in case we need to resume later
                chunked_bookmark = singer_utils.strptime_with_tz(
                    rec[replication_key])
                state = singer.write_bookmark(
                    state,
                    catalog_entry['tap_stream_id'],
                    'JobHighestBookmarkSeen',
                    singer_utils.strftime(chunked_bookmark))
                singer.write_state(state)
        # Before writing a bookmark, make sure Salesforce has not given us a
        # record with one outside our range
        elif replication_key_value and replication_key_value <= start_time:
            state = singer.write_bookmark(
                state,
                catalog_entry['tap_stream_id'],
                replication_key,
                rec[replication_key])
            singer.write_state(state)

        # Tables with no replication_key will send an
        # activate_version message for the next sync
    if not replication_key:
        singer.write_message(activate_version_message)
        state = singer.write_bookmark(
            state, catalog_entry['tap_stream_id'], 'version', None)

    # If pk_chunking is set, only write a bookmark at the end
    if sf.pk_chunking:
        # Write a bookmark with the highest value we've seen
        state = singer.write_bookmark(
            state,
            catalog_entry['tap_stream_id'],
            replication_key,
            singer_utils.strftime(chunked_bookmark))


def fix_record_anytype(rec, schema):
    """Modifies a record when the schema has no 'type' element due to a SF type of 'anyType.'
    Attempts to set the record's value for that element to an int, float, or string."""
    def try_cast(val, coercion):
        try:
            return coercion(val)
        except BaseException:
            return val

    for k, v in rec.items():
        typ = schema['properties'][k].get("type")
        format = schema['properties'][k].get("format")
        if typ is None:
            val = v
            val = try_cast(v, int)
            val = try_cast(v, float)
            if v in ["true", "false"]:
                val = (v == "true")

            if v == "":
                val = None

            rec[k] = val
        elif typ == 'number' or 'number' in typ:
            rec[k] = '' if v is not None and v == '-' else v
        elif format is not None and format == 'date-time':
            rec[k] = '' if v is not None and v.lower() == '<null>' else v

    return rec
