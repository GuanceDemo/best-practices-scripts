import gzip
import hashlib
import json
import logging
import time

from datakit import BaseDataKit
from dataway import DataWay


logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class ForwardPermanentError(RuntimeError):
    pass


# Compatibility for code that imported the old DataWay-specific name.
DataWayPermanentError = ForwardPermanentError


def parse_tags(value):
    tags = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        if ":" not in item:
            raise ValueError(f"Invalid TAGS item: {item!r}")
        key, tag_value = item.split(":", 1)
        key, tag_value = key.strip(), tag_value.strip()
        if not key or not tag_value:
            raise ValueError(f"Invalid TAGS item: {item!r}")
        tags[key] = tag_value
    return tags


def iter_log_records(body, object_name, max_json_document_bytes):
    # max_json_document_bytes is retained for configuration compatibility.
    # Log content is deliberately not parsed; each physical line is forwarded
    # exactly as text (apart from newline removal and UTF-8 replacement).
    del max_json_document_bytes
    stream = body
    if object_name.lower().endswith((".gz", ".gzip")):
        stream = gzip.GzipFile(fileobj=body)

    try:
        for raw_line in stream:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            yield line
    finally:
        if stream is not body:
            stream.close()


def stable_sample(record, sequence, sample_rate):
    if sample_rate >= 1:
        return True
    serialized = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(
        sequence.to_bytes(8, "big") + serialized
    ).digest()
    return int.from_bytes(digest[:8], "big") < int(
        sample_rate * (1 << 64)
    )


class LogSender:
    def __init__(self, config, token, sleep=time.sleep):
        self.config = config
        self.sleep = sleep
        self.direct_dataway = not bool(config.datakit_ip)
        if config.datakit_ip:
            self.client = BaseDataKit(
                host=config.datakit_ip,
                port=config.datakit_port,
                protocol=config.datakit_protocol,
                timeout=config.http_timeout,
                write_size=config.batch_size,
                raise_for_status=False,
            )
        else:
            self.client = DataWay(
                url=f"{config.dataway_url}?token={token}",
                timeout=config.http_timeout,
                write_size=config.batch_size,
                raise_for_status=False,
            )

    def close(self):
        self.client.close()

    def _headers(self):
        if self.direct_dataway and self.config.storage_index:
            return {"X-Storage-Index-Name": self.config.storage_index}
        return None

    def _query(self):
        if not self.direct_dataway and self.config.storage_index:
            return {"storage_index": self.config.storage_index}
        return None

    def send(self, points):
        if not points:
            return 0
        return self._send(points)

    def _send(self, points):
        last_error = None
        for attempt in range(self.config.max_retries + 1):
            try:
                status, response = self.client.write_by_category_many(
                    "logging",
                    points,
                    query=self._query(),
                    headers=self._headers(),
                )
                if 200 <= status < 300:
                    if (
                        isinstance(response, dict)
                        and response.get("error_code")
                    ):
                        logger.warning("Log endpoint response: %s", response)
                    return len(points)
                if status == 413 and len(points) > 1:
                    middle = len(points) // 2
                    return self._send(points[:middle]) + self._send(
                        points[middle:]
                    )
                if status not in RETRYABLE_STATUS_CODES:
                    raise ForwardPermanentError(
                        f"Log endpoint rejected request: status={status}, "
                        f"response={response!r}"
                    )
                last_error = RuntimeError(
                    f"retryable log endpoint response: status={status}, "
                    f"response={response!r}"
                )
            except ForwardPermanentError:
                raise
            except Exception as error:
                last_error = error

            if attempt < self.config.max_retries:
                delay = self.config.retry_base_seconds * (2 ** attempt)
                logger.warning(
                    "Log endpoint request failed; retry %d/%d in %.1fs: %s",
                    attempt + 1,
                    self.config.max_retries,
                    delay,
                    last_error,
                )
                self.sleep(delay)

        raise last_error


# Backward-compatible name for callers created before DataKit output was added.
DataWaySender = LogSender


class WafForwarder:
    def __init__(self, config, object_storage_client, sender):
        self.config = config
        self.object_storage_client = object_storage_client
        self.sender = sender
        self.common_tags = parse_tags(config.tags)

    def process(self, payload):
        totals = {
            "objects": 0,
            "input_records": 0,
            "sampled_out": 0,
            "records_written": 0,
        }
        events = payload if isinstance(payload, list) else [payload]
        try:
            for event in events:
                result = self._process_object(event)
                for key in totals:
                    totals[key] += result[key]
        finally:
            self.sender.close()
        totals["sample_rate"] = self.config.sample_rate
        return totals

    def _object_details(self, event):
        event_type = event.get("eventType") or event.get("type")
        if event_type and event_type != "com.oraclecloud.objectstorage.createobject":
            raise ValueError(f"Unsupported OCI event type: {event_type}")
        data = event.get("data") or {}
        details = data.get("additionalDetails") or {}
        namespace = details.get("namespace")
        bucket = details.get("bucketName")
        object_name = data.get("resourceName") or details.get("objectName")
        if not namespace:
            namespace = self.object_storage_client.get_namespace().data
        if not bucket or not object_name:
            raise ValueError(
                "OCI event must contain bucketName and resourceName"
            )
        if self.config.allowed_bucket and bucket != self.config.allowed_bucket:
            raise ValueError(f"Object bucket is not allowed: {bucket}")
        if self.config.object_prefix and not object_name.startswith(
            self.config.object_prefix
        ):
            raise ValueError(f"Object prefix is not allowed: {object_name}")
        return {
            "namespace": namespace,
            "bucket": bucket,
            "object_name": object_name,
        }

    def _response_body(self, response):
        data = response.data
        return getattr(data, "raw", data)

    def _process_object(self, event):
        details = self._object_details(event)
        response = self.object_storage_client.get_object(
            namespace_name=details["namespace"],
            bucket_name=details["bucket"],
            object_name=details["object_name"],
        )
        body = self._response_body(response)
        batch, batch_bytes = [], 0
        input_records = sampled_out = records_written = 0
        try:
            records = iter_log_records(
                body,
                details["object_name"],
                self.config.max_json_document_bytes,
            )
            for sequence, record in enumerate(records):
                input_records += 1
                if not stable_sample(
                    record, sequence, self.config.sample_rate
                ):
                    sampled_out += 1
                    continue
                point = self._to_point(record, details)
                point_bytes = len(
                    json.dumps(point, ensure_ascii=False, default=str).encode(
                        "utf-8"
                    )
                )
                if batch and (
                    len(batch) >= self.config.batch_size
                    or batch_bytes + point_bytes > self.config.batch_bytes
                ):
                    records_written += self.sender.send(batch)
                    batch, batch_bytes = [], 0
                batch.append(point)
                batch_bytes += point_bytes
            if batch:
                records_written += self.sender.send(batch)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

        logger.info(
            "OCI WAF object completed: bucket=%s object=%s input=%d "
            "sampled_out=%d written=%d",
            details["bucket"],
            details["object_name"],
            input_records, sampled_out, records_written,
        )
        return {
            "objects": 1,
            "input_records": input_records,
            "sampled_out": sampled_out,
            "records_written": records_written,
        }

    def _to_point(self, record, details):
        message = record if isinstance(record, str) else str(record)

        tags = {
            "cloud": "oci",
            "source": self.config.source,
            "service": self.config.service,
        }
        configured_tags = {
            "env": self.config.environment,
            "region": self.config.region,
            "waf_policy": self.config.waf_policy,
        }
        for key, value in configured_tags.items():
            if value:
                tags[key] = str(value)
        tags.update(self.common_tags)

        # Preserve the complete source record in message. Do not duplicate WAF
        # properties into tags or fields: downstream pipelines can parse
        # message when needed, keeping the forwarder schema-neutral.
        fields = {
            "message": message if isinstance(message, str) else json.dumps(
                message, ensure_ascii=False, separators=(",", ":")
            )
        }

        return {
            "measurement": self.config.source,
            "tags": tags,
            "fields": fields,
        }
