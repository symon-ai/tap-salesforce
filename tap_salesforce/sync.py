import time
import singer
import singer.utils as singer_utils
from singer import Transformer, metadata, metrics
from requests.exceptions import RequestException
from tap_salesforce.salesforce.bulk import Bulk

LOGGER = singer.get_logger()

BLACKLISTED_FIELDS = set(['attributes'])


def remove_blacklisted_fields(data):
    return {k: v for k, v in data.items() if k not in BLACKLISTED_FIELDS}

# pylint: disable=unused-argument


def transform_bulk_data_hook(data, typ, schema):
    global query01time
    query01time = time.time()
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
    
    global query01
    query01 = query01 + (time.time() - query01time)

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
    
    LOGGER.info("[TIMING] batchids " + str(batch_ids))

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
                LOGGER.info("[TIMING] syncing object")
                sync_records(sf, catalog_entry, state, counter)
            elif sf.source_type == 'report':
                LOGGER.info("[TIMING] syncing report")
                sync_report(sf, catalog_entry, state, counter)
            singer.write_state(state)
        except RequestException as ex:
            raise Exception("Error syncing {}: {} Response: {}".format(
                stream, ex, ex.response.text))
        except Exception as ex:
            raise Exception("Error syncing {}: {}".format(
                stream, ex)) from ex

        return counter

query01 = 0
query01time = time.time()
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

    querynum=1
    LOGGER.info("query" + str(querynum))
    LOGGER.info("state " + str(state))
    prequery = time.time()


    timep= time.time()


    query0 = 0
    query1 = 0
    query2 = 0
    query3 = 0
    query4 = 0
    query5 = 0
    query6 = 0
    query7 = 0
    global query01 
    query01 = 0


    for rec in sf.query(catalog_entry, state):
        qtime = time.time()

        counter.increment()
        # LOGGER.info("PRE TRNASFORM: " + str(rec))
        global query01time
        with Transformer(pre_hook=transform_bulk_data_hook) as transformer:
            rec = transformer.transform(rec, schema)
        # LOGGER.info("POST TRNASFORM: " + str(rec))

        query1 = query1+ (time.time() - qtime)
        qtime = time.time()

        rec = fix_record_anytype(rec, schema)
        query2 = query2+ (time.time() - qtime)
        qtime = time.time()

        singer.write_message(
            singer.RecordMessage(
                stream=(stream_id or stream_alias or stream),
                record=rec,
                version=stream_version,
                time_extracted=start_time))
        query3 = query3+ (time.time() - qtime)
        qtime = time.time()

        replication_key_value = replication_key and singer_utils.strptime_with_tz(
            rec[replication_key])
        query4 = query4+ (time.time() - qtime)
        qtime = time.time()

        if sf.pk_chunking:
            if replication_key_value and replication_key_value <= start_time and replication_key_value > chunked_bookmark:
                # Replace the highest seen bookmark and save the state in case we need to resume later
                chunked_bookmark = singer_utils.strptime_with_tz(
                    rec[replication_key])
                query5 = query5+ (time.time() - qtime)
                qtime = time.time()

                state = singer.write_bookmark(
                    state,
                    catalog_entry['tap_stream_id'],
                    'JobHighestBookmarkSeen',
                    singer_utils.strftime(chunked_bookmark))
                query6 = query6+ (time.time() - qtime)
                qtime = time.time()

                singer.write_state(state)
                query7 = query7+ (time.time() - qtime)
                qtime = time.time()
        # Before writing a bookmark, make sure Salesforce has not given us a
        # record with one outside our range
        elif replication_key_value and replication_key_value <= start_time:
            state = singer.write_bookmark(
                state,
                catalog_entry['tap_stream_id'],
                replication_key,
                rec[replication_key])
            query6 = query6+ (time.time() - qtime)
            qtime = time.time()

            singer.write_state(state)
            query7 = query7+ (time.time() - qtime)
            qtime = time.time()

    LOGGER.info('{}: {}'.format("q1 transform ", query1))
    LOGGER.info('{}: {}'.format("q01 transformbulkdata ", query01))
    LOGGER.info('{}: {}'.format("q2 fixrecordtype ", query2))
    LOGGER.info('{}: {}'.format("q3 writemessage ", query3))
    LOGGER.info('{}: {}'.format("q4 replication_key_value ", query4))
    LOGGER.info('{}: {}'.format("q5 ifchunkedbookmore ", query5))
    LOGGER.info('{}: {}'.format("q6 writebookmark ", query6))
    LOGGER.info('{}: {}'.format("q7 write_state ", query7))




        # Tables with no replication_key will send an
        # activate_version message for the next sync
    LOGGER.info('{}: {}'.format("notreplekey", time.time() - timep))
    timep= time.time()
    if not replication_key:
        singer.write_message(activate_version_message)
        state = singer.write_bookmark(
            state, catalog_entry['tap_stream_id'], 'version', None)

    LOGGER.info('{}: {}'.format("pkchunk", time.time() - timep))
    timep= time.time()
    # If pk_chunking is set, only write a bookmark at the end
    if sf.pk_chunking:
        # Write a bookmark with the highest value we've seen
        state = singer.write_bookmark(
            state,
            catalog_entry['tap_stream_id'],
            replication_key,
            singer_utils.strftime(chunked_bookmark))

    LOGGER.info('{}: {}'.format("exit", time.time() - timep))
    timep= time.time()


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
        if schema['properties'][k].get("type") is None:
            val = v
            val = try_cast(v, int)
            val = try_cast(v, float)
            if v in ["true", "false"]:
                val = (v == "true")

            if v == "":
                val = None

            rec[k] = val

    return rec
