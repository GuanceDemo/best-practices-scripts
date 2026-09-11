import gzip
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


FUNCTION_DIR = os.path.dirname(os.path.dirname(__file__))
if FUNCTION_DIR not in sys.path:
    sys.path.insert(0, FUNCTION_DIR)

from config import Config
from forwarder import (
    DataWayPermanentError,
    LogSender,
    WafForwarder,
    iter_log_records,
)


def make_config(**overrides):
    values = {
        "datakit_ip": "",
        "datakit_port": 9529,
        "datakit_protocol": "http",
        "dataway_url": "https://dataway.example",
        "dataway_token": "token",
        "dataway_token_secret_ocid": "",
        "storage_index": "security",
        "source": "oci_waf",
        "service": "oci_waf",
        "environment": "prod",
        "region": "ap-singapore-1",
        "waf_policy": "prod-waf",
        "tags": "team:security",
        "allowed_bucket": "oci-waf-logs",
        "object_prefix": "waf-logs/",
        "batch_size": 2,
        "batch_bytes": 1024 * 1024,
        "http_timeout": 15,
        "max_retries": 2,
        "retry_base_seconds": 0,
        "sample_rate": 1,
        "max_json_document_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return Config(**values)


class FakeObjectClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=SimpleNamespace(raw=io.BytesIO(self.payload))
        )


class FakeSender:
    def __init__(self):
        self.batches = []
        self.closed = False

    def send(self, points):
        self.batches.append(points)
        return len(points)

    def close(self):
        self.closed = True


class RecordParsingTests(unittest.TestCase):
    def test_streams_gzip_json_lines(self):
        payload = gzip.compress(b'{"action":"BLOCK"}\n{"action":"LOG"}\n')
        records = list(iter_log_records(io.BytesIO(payload), "waf.json.gz", 1024))
        self.assertEqual(
            ['{"action":"BLOCK"}', '{"action":"LOG"}'], records
        )

    def test_does_not_parse_json_array(self):
        payload = b'[{"action":"BLOCK"},{"action":"LOG"}]'
        records = list(iter_log_records(io.BytesIO(payload), "waf.json", 1024))
        self.assertEqual([payload.decode()], records)


class ForwarderTests(unittest.TestCase):
    def test_processes_event_batches_and_fields(self):
        lines = [
            {
                "timestamp": "2026-08-11T10:00:00Z",
                "action": "BLOCK",
                "logType": "PROTECTION_RULES",
                "clientAddress": "203.0.113.10",
                "requestUrl": "/login",
            },
            {"action": "LOG", "requestUrl": "/health"},
            {"action": "DETECT", "requestUrl": "/admin"},
        ]
        payload = gzip.compress(
            b"\n".join(json.dumps(item).encode() for item in lines) + b"\n"
        )
        object_client = FakeObjectClient(payload)
        sender = FakeSender()
        forwarder = WafForwarder(make_config(), object_client, sender)
        event = {
            "eventType": "com.oraclecloud.objectstorage.createobject",
            "eventID": "event-1",
            "data": {
                "resourceName": "waf-logs/waf.json.gz",
                "additionalDetails": {
                    "namespace": "namespace",
                    "bucketName": "oci-waf-logs",
                    "eTag": "etag-1",
                },
            },
        }

        result = forwarder.process(event)

        self.assertEqual(3, result["records_written"])
        self.assertEqual([2, 1], [len(batch) for batch in sender.batches])
        self.assertTrue(sender.closed)
        point = sender.batches[0][0]
        self.assertEqual("oci_waf", point["measurement"])
        self.assertEqual("security", point["tags"]["team"])
        self.assertNotIn("action", point["tags"])
        self.assertNotIn("log_type", point["tags"])
        original = json.loads(point["fields"]["message"])
        self.assertEqual("203.0.113.10", original["clientAddress"])
        self.assertEqual("/login", original["requestUrl"])
        self.assertEqual({"message"}, set(point["fields"]))
        self.assertNotIn("timestamp", point)

    def test_rejects_wrong_bucket(self):
        forwarder = WafForwarder(
            make_config(), FakeObjectClient(b""), FakeSender()
        )
        with self.assertRaisesRegex(ValueError, "not allowed"):
            forwarder.process({
                "data": {
                    "resourceName": "waf-logs/a.json",
                    "additionalDetails": {
                        "namespace": "ns",
                        "bucketName": "other",
                    },
                }
            })


class SenderTests(unittest.TestCase):
    def test_dataway_uses_storage_index_header(self):
        sender = LogSender(make_config(), "token", sleep=lambda _: None)
        sender.client = mock.Mock()
        sender.client.write_by_category_many.return_value = (200, b"")
        points = [{"measurement": "x", "tags": {}, "fields": {"message": "x"}}]

        self.assertEqual(1, sender.send(points))
        self.assertNotIn("status", points[0]["fields"])

        _, kwargs = sender.client.write_by_category_many.call_args
        self.assertEqual(
            {"X-Storage-Index-Name": "security"}, kwargs["headers"]
        )
        self.assertIsNone(kwargs["query"])

    def test_datakit_uses_storage_index_query(self):
        sender = LogSender(
            make_config(datakit_ip="10.0.0.10"), "", sleep=lambda _: None
        )
        sender.client = mock.Mock()
        sender.client.write_by_category_many.return_value = (200, b"")

        points = [{"fields": {"message": "x"}}]
        self.assertEqual(1, sender.send(points))
        self.assertNotIn("status", points[0]["fields"])

        _, kwargs = sender.client.write_by_category_many.call_args
        self.assertEqual({"storage_index": "security"}, kwargs["query"])
        self.assertIsNone(kwargs["headers"])

    def test_retries_503(self):
        sender = LogSender(make_config(), "token", sleep=lambda _: None)
        sender.client = mock.Mock()
        sender.client.write_by_category_many.side_effect = [
            (503, b"busy"),
            (200, b""),
        ]
        self.assertEqual(1, sender.send([{}]))
        self.assertEqual(2, sender.client.write_by_category_many.call_count)

    def test_does_not_retry_400(self):
        sender = LogSender(make_config(), "token", sleep=lambda _: None)
        sender.client = mock.Mock()
        sender.client.write_by_category_many.return_value = (400, b"bad")
        with self.assertRaises(DataWayPermanentError):
            sender.send([{}])
        self.assertEqual(1, sender.client.write_by_category_many.call_count)

    def test_splits_413_batch(self):
        sender = LogSender(make_config(), "token", sleep=lambda _: None)
        sender.client = mock.Mock()
        sender.client.write_by_category_many.side_effect = [
            (413, b"large"),
            (200, b""),
            (200, b""),
        ]
        self.assertEqual(4, sender.send([{}, {}, {}, {}]))
        self.assertEqual(3, sender.client.write_by_category_many.call_count)


class ConfigTests(unittest.TestCase):
    def test_datakit_does_not_require_dataway_configuration(self):
        env = {
            "DATAKIT_IP": "10.0.0.10",
            "DATAKIT_PORT": "9529",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual("10.0.0.10", config.datakit_ip)
        self.assertEqual("", config.dataway_url)
        self.assertEqual("", config.dataway_token)

    def test_rejects_invalid_sample_rate(self):
        env = {
            "DATAWAY_URL": "https://dataway.example",
            "DATAWAY_TOKEN": "token",
            "LOG_SAMPLE_RATE": "0",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
