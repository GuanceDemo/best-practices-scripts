import os
from dataclasses import dataclass


def _env(name, default=""):
    return os.getenv(name, default).strip()


def _int_env(name, default, minimum=1):
    value = int(_env(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _float_env(name, default, minimum=None, maximum=None):
    value = float(_env(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    datakit_ip: str
    datakit_port: int
    datakit_protocol: str
    dataway_url: str
    dataway_token: str
    dataway_token_secret_ocid: str
    storage_index: str
    source: str
    service: str
    environment: str
    region: str
    waf_policy: str
    tags: str
    allowed_bucket: str
    object_prefix: str
    batch_size: int
    batch_bytes: int
    http_timeout: int
    max_retries: int
    retry_base_seconds: float
    sample_rate: float
    max_json_document_bytes: int

    @classmethod
    def from_env(cls):
        config = cls(
            datakit_ip=_env("DATAKIT_IP"),
            datakit_port=_int_env("DATAKIT_PORT", 9529),
            datakit_protocol=_env("DATAKIT_PROTOCOL", "http"),
            dataway_url=_env("DATAWAY_URL"),
            dataway_token=_env("DATAWAY_TOKEN", _env("WORKSPACE_TOKEN")),
            dataway_token_secret_ocid=_env("DATAWAY_TOKEN_SECRET_OCID"),
            storage_index=_env("STORAGE_INDEX"),
            source=_env("SOURCE", "oci_waf"),
            service=_env("SERVICE", "oci_waf"),
            environment=_env("ENV", ""),
            region=_env("OCI_REGION", ""),
            waf_policy=_env("WAF_POLICY", ""),
            tags=_env("TAGS", ""),
            allowed_bucket=_env("ALLOWED_BUCKET", ""),
            object_prefix=_env("OBJECT_PREFIX", ""),
            batch_size=_int_env("FORWARD_BATCH_SIZE", 500),
            batch_bytes=_int_env("FORWARD_BATCH_BYTES", 1024 * 1024),
            http_timeout=_int_env("HTTP_TIMEOUT", 15),
            max_retries=_int_env("MAX_RETRIES", 3, minimum=0),
            retry_base_seconds=_float_env(
                "RETRY_BASE_SECONDS", 1, minimum=0
            ),
            sample_rate=_float_env(
                "LOG_SAMPLE_RATE", 1, minimum=0.01, maximum=1
            ),
            max_json_document_bytes=_int_env(
                "MAX_JSON_DOCUMENT_BYTES", 10 * 1024 * 1024
            ),
        )
        if config.datakit_protocol not in ("http", "https"):
            raise ValueError("DATAKIT_PROTOCOL must be http or https")
        if not config.datakit_ip and not config.dataway_url:
            raise ValueError("DATAKIT_IP or DATAWAY_URL is required")
        if not config.datakit_ip and not (
            config.dataway_token or config.dataway_token_secret_ocid
        ):
            raise ValueError(
                "DATAWAY_TOKEN or DATAWAY_TOKEN_SECRET_OCID is required"
            )
        return config
