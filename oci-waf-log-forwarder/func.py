import base64
import io
import json
import logging
import os

from fdk import response

from config import Config
from forwarder import LogSender, WafForwarder


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

_runtime = None


def _resource_principal_signer():
    import oci

    return oci.auth.signers.get_resource_principals_signer()


def _resolve_token(config, signer):
    if config.datakit_ip:
        return ""
    if config.dataway_token:
        return config.dataway_token
    import oci

    client = oci.secrets.SecretsClient(config={}, signer=signer)
    bundle = client.get_secret_bundle(
        secret_id=config.dataway_token_secret_ocid
    ).data
    encoded = bundle.secret_bundle_content.content
    return base64.b64decode(encoded).decode("utf-8").strip()


def _build_runtime():
    import oci

    config = Config.from_env()
    signer = _resource_principal_signer()
    object_client = oci.object_storage.ObjectStorageClient(
        config={}, signer=signer
    )
    token = _resolve_token(config, signer)
    sender = LogSender(config, token)
    return WafForwarder(config, object_client, sender)


def get_runtime():
    global _runtime
    if _runtime is None:
        _runtime = _build_runtime()
    return _runtime


def handler(ctx, data: io.BytesIO = None):
    try:
        payload = json.loads((data or io.BytesIO(b"{}")).getvalue())
        result = get_runtime().process(payload)
        logger.info("OCI WAF forwarding completed: %s", result)
        return response.Response(
            ctx,
            response_data=json.dumps(result),
            headers={"Content-Type": "application/json"},
        )
    except Exception:
        logger.exception("OCI WAF forwarding failed")
        raise
