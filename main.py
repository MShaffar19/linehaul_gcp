import arrow
import cattr

import datetime
import os
import json
import gzip
import sys
import tempfile

from collections import defaultdict
from tempfile import NamedTemporaryFile
from contextlib import ExitStack
from pathlib import Path

from linehaul.events.parser import parse, Download, Simple

from google.cloud import bigquery, storage

_cattr = cattr.Converter()
_cattr.register_unstructure_hook(
    arrow.Arrow, lambda o: o.format("YYYY-MM-DD HH:mm:ss ZZ")
)


DATASET = os.environ.get("BIGQUERY_DATASET")
SIMPLE_TABLE = os.environ.get("BIGQUERY_SIMPLE_TABLE")
DOWNLOAD_TABLE = os.environ.get("BIGQUERY_DOWNLOAD_TABLE")

prefix = {Simple.__name__: "simple_requests", Download.__name__: "file_downloads"}


def process_fastly_log(data, context):
    storage_client = storage.Client()
    bigquery_client = bigquery.Client()
    identifier = os.path.basename(data["name"]).split("-", 3)[-1].rstrip(".log.gz")
    default_partition = datetime.datetime.utcnow().strftime("%Y%m%d")

    bob_logs_log_blob = storage_client.bucket(data["bucket"]).get_blob(data["name"])
    if bob_logs_log_blob is None:
        return  # This has already been processed?

    unprocessed_lines = 0
    simple_lines = 0
    download_lines = 0

    with ExitStack() as stack:
        input_file_obj = stack.enter_context(NamedTemporaryFile())
        bob_logs_log_blob.download_to_file(input_file_obj)
        input_file_obj.flush()

        input_file = stack.enter_context(gzip.open(input_file_obj.name, "rb"))
        unprocessed_file = stack.enter_context(NamedTemporaryFile())
        simple_results_file = stack.enter_context(NamedTemporaryFile())
        download_results_file = stack.enter_context(NamedTemporaryFile())

        for line in input_file:
            try:
                res = parse(line.decode())
                if res is not None:
                    if res.__class__.__name__ == Simple.__name__:
                        simple_results_file.write(
                            json.dumps(_cattr.unstructure(res)).encode() + b"\n"
                        )
                        simple_lines += 1
                    elif res.__class__.__name__ == Download.__name__:
                        download_results_file.write(
                            json.dumps(_cattr.unstructure(res)).encode() + b"\n"
                        )
                        download_lines += 1
                    else:
                        unprocessed_file.write(line)
                        unprocessed_lines += 1
                else:
                    unprocessed_file.write(line)
                    unprocessed_lines += 1
            except Exception as e:
                unprocessed_file.write(line)
                unprocessed_lines += 1

        total = unprocessed_lines + simple_lines + download_lines
        print(
            f"Processed gs://{data['bucket']}/{data['name']}: {total} lines, {simple_lines} simple_requests, {download_lines} file_downloads, {unprocessed_lines} unprocessed"
        )

        dataset_ref = bigquery_client.dataset(DATASET)

        job_config = bigquery.LoadJobConfig()
        job_config.source_format = bigquery.SourceFormat.NEWLINE_DELIMITED_JSON
        job_config.ignore_unknown_values = True

        if download_lines > 0:
            load_job = bigquery_client.load_table_from_file(
                download_results_file,
                dataset_ref.table(DOWNLOAD_TABLE),
                job_id_prefix="linehaul_file_downloads",
                location="US",
                job_config=job_config,
                rewind=True,
            )
            load_job.result()
            print(f"Loaded {load_job.output_rows} rows into {DATASET}:{DOWNLOAD_TABLE}")

        if simple_lines > 0:
            load_job = bigquery_client.load_table_from_file(
                simple_results_file,
                dataset_ref.table(SIMPLE_TABLE),
                job_id_prefix="linehaul_file_downloads",
                location="US",
                job_config=job_config,
                rewind=True,
            )
            load_job.result()
            print(f"Loaded {load_job.output_rows} rows into {DATASET}:{SIMPLE_TABLE}")

        bucket = storage_client.bucket(os.environ.get("RESULT_BUCKET"))
        if unprocessed_lines > 0:
            blob = bucket.blob(f"unprocessed/{default_partition}/{identifier}.txt")
            try:
                blob.upload_from_file(unprocessed_file, rewind=True)
            except:
                # Be opprotunistic about unprocessed files...
                pass

        bob_logs_log_blob.delete()
