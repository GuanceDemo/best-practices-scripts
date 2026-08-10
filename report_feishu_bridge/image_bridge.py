from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import mimetypes
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import error, parse, request

# =============================================================================
# 用户配置区：复制脚本后通常只需要修改这里
# =============================================================================

USER_CONFIG = {
    # 飞书/Lark 自建应用：用于上传图片并获取 image_key。
    # 国内飞书应用填飞书开放平台 App ID；海外 Lark 应用填 Lark Developer Console App ID。
    "lark_app_id": "",

    # 飞书/Lark 自建应用密钥：和 lark_app_id 配套。
    "lark_app_secret": "",

    # OpenAPI 域名：国内飞书使用 https://open.feishu.cn；海外 Lark 使用 https://open.larksuite.com。
    "lark_openapi_base_url": "https://open.feishu.cn",

    # 飞书/Lark 群自定义机器人 Webhook：用于把卡片发送到目标群。
    "feishu_bot_webhook": "",

    # 群机器人签名密钥：未开启签名校验时留空；开启后填写密钥。
    "feishu_bot_secret": "",

    # 是否仅演练流程不真实发送：正式使用保持 False，排查问题时可临时改为 True。
    "dry_run": False,

    # 是否打印脱敏后的完整 payload：排查问题时临时改为 True。
    "debug_payload": False,

    # 入口鉴权密钥：为空表示不校验；填写后 Webhook payload/query/header 中必须带同值。
    "shared_secret": "",

    # 图片 URL 字段优先级：脚本会先按这些路径查找，再递归扫描所有疑似图片 URL。
    "image_url_paths": [
        "dashboardSnapshotUrl",
        "chartSnapshotUrls",
        "data.reportImageUrl",
        "data.image_url",
        "image_url",
        "reportImageUrl",
        "screenshotUrl",
    ],

    # 图片下载超时时间，单位秒。
    "download_timeout_seconds": 20,

    # 调用飞书/Lark OpenAPI 和群机器人 Webhook 的超时时间，单位秒。
    "openapi_timeout_seconds": 20,

    # 单张图片最大体积，默认 10 MB。
    "max_image_bytes": 10 * 1024 * 1024,

    # 消息形态：card 发送卡片并嵌入截图；image 只发送图片。
    "message_mode": "card",

    # 没有拿到图片 URL 时是否仍发送一张说明卡片。
    "send_notice_when_no_image": True,

    # image_key 记录裁剪：图片下载不落本地磁盘，这里只裁剪脚本保存的最近 image_key 记录。
    "image_key_cleanup_enabled": True,

    # image_key 记录超过该数量时触发裁剪。
    "image_key_cleanup_max": 10,

    # 触发裁剪后保留最新多少条 image_key 记录。
    "image_key_cleanup_keep": 5,
}

# Reusable report bridge:
# scheduled report custom webhook -> download report image -> upload to
# Feishu/Lark image resource -> send card or image to group custom robot.
#
# Keep customer-specific values in USER_CONFIG above. This script intentionally
# does not read Func environment variables, so cloned scripts cannot be affected
# by stale configuration left in the runtime.

try:
    DFF
except NameError:
    class _LocalDFF:
        class _Store:
            _data: Dict[str, Any] = {}

            @classmethod
            def get(cls, key, scope=None):
                return cls._data.get("%s:%s" % (scope or "default", key))

            @classmethod
            def set(cls, key, value, expires=None, scope=None):
                cls._data["%s:%s" % (scope or "default", key)] = value
                return True

        @staticmethod
        def API(_name, **_kwargs):
            def decorator(func):
                return func
            return decorator

        STORE = _Store

    DFF = _LocalDFF()


IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp)(\?|#|$)", re.I)
IMAGE_FIELD_RE = re.compile(r"(image|img|pic|picture|screenshot|snapshot|chart|report)", re.I)
URL_RE = re.compile(r"^https?://", re.I)

SENSITIVE_KEY_RE = re.compile(r"(secret|token|password|sign|authorization|webhook|api_key|access_key)", re.I)
DEFAULT_IMAGE_PATHS = [
    "image_url",
    "imageUrl",
    "report_image_url",
    "reportImageUrl",
    "screenshot_url",
    "screenshotUrl",
    "snapshot_url",
    "snapshotUrl",
    "chart_image_url",
    "chartImageUrl",
    "pic_url",
    "picUrl",
    "content.image_url",
    "content.imageUrl",
    "data.image_url",
    "data.imageUrl",
    "data.report_image_url",
    "data.reportImageUrl",
    "event.image_url",
    "event.imageUrl",
]


class BridgeError(Exception):
    pass


def setting_str(config_key: str, default: str = "") -> str:
    value = USER_CONFIG.get(config_key, default)
    if value is None:
        return default
    return str(value)


def setting_bool(config_key: str, default: bool = False) -> bool:
    value = USER_CONFIG.get(config_key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def setting_int(config_key: str, default: int) -> int:
    value = USER_CONFIG.get(config_key, default)
    try:
        return int(value)
    except Exception:
        return default


def setting_json(config_key: str, default: Any) -> Any:
    value = USER_CONFIG.get(config_key, default)
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except Exception:
        if isinstance(default, list):
            return [item.strip() for item in str(value).split(",") if item.strip()]
        return default


def config() -> Dict[str, Any]:
    return {
        "dry_run": setting_bool("dry_run", False),
        "debug_payload": setting_bool("debug_payload", True),
        "shared_secret": setting_str("shared_secret", ""),
        "image_url_paths": setting_json("image_url_paths", DEFAULT_IMAGE_PATHS),
        "download_timeout_seconds": setting_int("download_timeout_seconds", 20),
        "openapi_timeout_seconds": setting_int("openapi_timeout_seconds", 20),
        "max_image_bytes": setting_int("max_image_bytes", 10 * 1024 * 1024),
        "send_text_when_no_image": setting_bool("send_notice_when_no_image", True),
        "message_mode": setting_str("message_mode", "card").strip().lower() or "card",
        "image_key_cleanup_enabled": setting_bool("image_key_cleanup_enabled", True),
        "image_key_cleanup_max": setting_int("image_key_cleanup_max", 10),
        "image_key_cleanup_keep": setting_int("image_key_cleanup_keep", 5),
        "feishu": {
            "app_id": setting_str("lark_app_id", ""),
            "app_secret": setting_str("lark_app_secret", ""),
            "api_base": setting_str("lark_openapi_base_url", "https://open.feishu.cn").rstrip("/"),
            "bot_webhook": setting_str("feishu_bot_webhook", ""),
            "bot_secret": setting_str("feishu_bot_secret", ""),
        },
    }


def compact(value: Any, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                out[key] = "***"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value[:80]]
    if isinstance(value, str) and len(value) > 800:
        return value[:800] + "...<truncated>"
    return value


def log_info(step: str, **fields: Any) -> None:
    print("[report-card-bridge] " + json.dumps({"step": step, **redact(fields)}, ensure_ascii=False, default=str))


def normalize_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"raw": raw}

    for key in ("body", "__body", "payload"):
        body = raw.get(key)
        if isinstance(body, str):
            body = body.strip()
            if body:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        merged = dict(raw)
                        merged[key] = parsed
                        if len(raw) == 1:
                            return parsed
                        return merged
                except Exception:
                    pass
    return raw


def get_by_path(data: Any, path: str) -> Any:
    current = data
    for part in str(path).split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def looks_like_image_url(field_path: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not URL_RE.search(text):
        return False
    return bool(IMAGE_EXT_RE.search(text) or IMAGE_FIELD_RE.search(field_path))


def walk_urls(data: Any, path: str = "") -> Iterable[Tuple[str, str]]:
    if isinstance(data, dict):
        for key, value in data.items():
            next_path = key if not path else path + "." + str(key)
            yield from walk_urls(value, next_path)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            next_path = "%s.%s" % (path, index) if path else str(index)
            yield from walk_urls(value, next_path)
    elif looks_like_image_url(path, data):
        yield path, str(data).strip()


def find_image_urls(payload: Dict[str, Any], paths: List[str]) -> List[Dict[str, str]]:
    seen = set()
    found = []

    for path in paths or []:
        value = get_by_path(payload, path)
        if looks_like_image_url(path, value) and value not in seen:
            seen.add(value)
            found.append({"path": str(path), "url": str(value).strip(), "source": "configured_path"})

    for path, url in walk_urls(payload):
        if url not in seen:
            seen.add(url)
            found.append({"path": path, "url": url, "source": "recursive_scan"})

    return found


def verify_bridge_secret(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    expected = cfg.get("shared_secret") or ""
    if not expected:
        return

    candidates = [
        payload.get("bridge_secret"),
        payload.get("token"),
        payload.get("secret"),
        get_by_path(payload, "headers.x-report-bridge-secret"),
        get_by_path(payload, "headers.X-Report-Bridge-Secret"),
        get_by_path(payload, "query.bridge_secret"),
    ]
    if expected not in [str(item or "") for item in candidates]:
        raise BridgeError("invalid shared_secret")


def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> Dict[str, Any]:
    data = None
    final_headers = {"Content-Type": "application/json"}
    if headers:
        final_headers.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, method=method, headers=final_headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise BridgeError("HTTP %s %s: %s" % (exc.code, exc.reason, raw))
    return json.loads(raw) if raw else {}


def download_image(url: str, timeout: int, max_bytes: int) -> Tuple[bytes, str, str]:
    req = request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "Report-Feishu-Card-Bridge/1.0",
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            chunks = []
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise BridgeError("image exceeds max_image_bytes: %s > %s" % (total, max_bytes))
                chunks.append(chunk)
    except error.HTTPError as exc:
        raise BridgeError("download image failed: HTTP %s %s" % (exc.code, exc.reason))

    data = b"".join(chunks)
    if not content_type:
        content_type = mimetypes.guess_type(parse.urlparse(url).path)[0] or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise BridgeError("downloaded URL is not image content: %s" % content_type)

    ext = mimetypes.guess_extension(content_type) or ".png"
    filename = "report-snapshot-%s%s" % (int(time.time()), ext)
    return data, filename, content_type


def multipart_body(fields: Dict[str, str], files: Dict[str, Tuple[str, bytes, str]]) -> Tuple[bytes, str]:
    boundary = "----report-feishu-bridge-" + uuid.uuid4().hex
    parts: List[bytes] = []
    for name, value in fields.items():
        parts.append(("--%s\r\n" % boundary).encode("utf-8"))
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for name, file_info in files.items():
        filename, data, content_type = file_info
        parts.append(("--%s\r\n" % boundary).encode("utf-8"))
        disposition = 'Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename)
        parts.append(disposition.encode("utf-8"))
        parts.append(("Content-Type: %s\r\n\r\n" % content_type).encode("utf-8"))
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def get_tenant_access_token(cfg: Dict[str, Any]) -> str:
    feishu = cfg.get("feishu") or {}
    app_id = feishu.get("app_id") or ""
    app_secret = feishu.get("app_secret") or ""
    if not app_id or not app_secret:
        raise BridgeError("missing USER_CONFIG lark_app_id or lark_app_secret")

    url = feishu["api_base"] + "/open-apis/auth/v3/tenant_access_token/internal"
    resp = http_json(
        "POST",
        url,
        {"app_id": app_id, "app_secret": app_secret},
        timeout=cfg.get("openapi_timeout_seconds", 20),
    )
    if resp.get("code") != 0:
        raise BridgeError("tenant_access_token failed: %s" % resp)
    token = resp.get("tenant_access_token")
    if not token:
        raise BridgeError("tenant_access_token missing in Feishu response")
    return token


def upload_image(cfg: Dict[str, Any], token: str, image_bytes: bytes, filename: str, content_type: str) -> str:
    if cfg.get("dry_run"):
        log_info("dry_run.upload_image", filename=filename, content_type=content_type, bytes=len(image_bytes))
        return "img_dryrun_" + uuid.uuid4().hex[:12]

    body, content_type_header = multipart_body(
        {"image_type": "message"},
        {"image": (filename, image_bytes, content_type)},
    )
    req = request.Request(
        (cfg.get("feishu") or {})["api_base"] + "/open-apis/im/v1/images",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": content_type_header,
        },
    )
    try:
        with request.urlopen(req, timeout=cfg.get("openapi_timeout_seconds", 20)) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise BridgeError("upload image failed: HTTP %s %s: %s" % (exc.code, exc.reason, raw))

    data = json.loads(raw) if raw else {}
    if data.get("code") != 0:
        raise BridgeError("upload image failed: %s" % data)
    image_key = ((data.get("data") or {}).get("image_key") or "")
    if not image_key:
        raise BridgeError("image_key missing in Feishu response")
    return image_key


def store_get_json(key: str, default: Any) -> Any:
    try:
        value = DFF.STORE.get(key, scope="report_feishu_bridge")
        if value in (None, ""):
            return default
        if isinstance(value, str):
            return json.loads(value)
        return value
    except Exception as exc:
        log_info("store.get.failed", key=key, error=str(exc))
        return default


def store_set_json(key: str, value: Any) -> bool:
    try:
        DFF.STORE.set(key, value, scope="report_feishu_bridge")
        return True
    except Exception as exc:
        log_info("store.set.failed", key=key, error=str(exc))
        return False


def remember_image_key(cfg: Dict[str, Any], image_key: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if not cfg.get("image_key_cleanup_enabled"):
        return {"enabled": False}

    max_items = max(1, int(cfg.get("image_key_cleanup_max") or 10))
    keep_items = max(1, min(int(cfg.get("image_key_cleanup_keep") or 5), max_items))
    registry = store_get_json("uploaded_image_keys", [])
    if not isinstance(registry, list):
        registry = []

    registry.append({
        "image_key": image_key,
        "ts": int(time.time()),
        "meta": {
            "title": meta.get("title"),
            "image_url_path": meta.get("image_url_path"),
            "image_bytes": meta.get("image_bytes"),
        },
    })

    removed = []
    if len(registry) > max_items:
        removed = registry[:-keep_items]
        registry = registry[-keep_items:]
    saved = store_set_json("uploaded_image_keys", registry)
    return {
        "enabled": True,
        "saved": saved,
        "max": max_items,
        "keep": keep_items,
        "current": len(registry),
        "forgotten": len(removed),
    }


def feishu_robot_sign(secret: str, timestamp: str) -> str:
    string_to_sign = "%s\n%s" % (timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_robot_message(cfg: Dict[str, Any], msg_type: str, content: Dict[str, Any]) -> Dict[str, Any]:
    if cfg.get("dry_run"):
        log_info("dry_run.send_robot", msg_type=msg_type, content=content)
        return {"dry_run": True, "msg_type": msg_type}

    feishu = cfg.get("feishu") or {}
    webhook = feishu.get("bot_webhook") or ""
    if not webhook:
        raise BridgeError("missing USER_CONFIG feishu_bot_webhook")

    if msg_type == "interactive":
        body: Dict[str, Any] = {"msg_type": msg_type, "card": content}
    else:
        body = {"msg_type": msg_type, "content": content}
    secret = feishu.get("bot_secret") or ""
    if secret:
        timestamp = str(int(time.time()))
        body["timestamp"] = timestamp
        body["sign"] = feishu_robot_sign(secret, timestamp)

    resp = http_json("POST", webhook, body, timeout=cfg.get("openapi_timeout_seconds", 20))
    if resp.get("StatusCode") not in (None, 0) or resp.get("code") not in (None, 0):
        raise BridgeError("Feishu robot webhook failed: %s" % resp)
    return resp


def report_title(payload: Dict[str, Any]) -> str:
    for path in ("title", "report_title", "reportTitle", "data.title", "event.title", "content.title"):
        value = get_by_path(payload, path)
        if value:
            return compact(value, 80)
    return "定时报告"


def first_value(payload: Dict[str, Any], paths: Iterable[str], default: str = "") -> str:
    for path in paths:
        value = get_by_path(payload, path)
        if value not in (None, "", []):
            return compact(value, 180)
    return default


def timezone_offset_hours(tz: str) -> int:
    text = (tz or "").strip()
    if text in ("Asia/Shanghai", "Asia/Chongqing", "Asia/Hong_Kong", "Asia/Singapore", "UTC+8", "GMT+8"):
        return 8
    if text in ("UTC", "Etc/UTC", "GMT", "Z"):
        return 0
    match = re.match(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        return sign * int(match.group(2))
    return 8


def format_ms(ms: Any, tz: str) -> str:
    try:
        seconds = int(ms) / 1000.0
    except Exception:
        return compact(ms, 40)
    dt = datetime.datetime.utcfromtimestamp(seconds) + datetime.timedelta(hours=timezone_offset_hours(tz))
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def report_time_range(payload: Dict[str, Any]) -> str:
    tz = first_value(payload, ("timezone", "data.timezone"), "Asia/Shanghai")
    value = get_by_path(payload, "timeRange") or get_by_path(payload, "data.timeRange")
    if isinstance(value, list) and len(value) >= 2:
        return "%s ~ %s" % (format_ms(value[0], tz), format_ms(value[1], tz))
    return first_value(payload, ("time_range", "timeRangeText", "queryTime", "data.time_range", "data.queryTime"), "")


def build_report_card(payload: Dict[str, Any], image_key: str) -> Dict[str, Any]:
    title = report_title(payload)
    time_range = report_time_range(payload)
    dashboard_name = first_value(payload, ("dashboardName", "dashboard_name", "data.dashboardName"), "")
    workspace_name = first_value(payload, ("workspaceName", "workspace_name", "data.workspaceName"), "")
    node_info = first_value(payload, ("nodeInfo", "node_info", "nodeName", "node_name", "data.nodeInfo"), "")
    link_url = first_value(payload, ("linkUrl", "link_url", "dashboardUrl", "dashboard_url", "data.linkUrl"), "")
    content = first_value(payload, ("content", "message", "data.content"), "")

    header_title = "【报告通知】"
    if time_range:
        header_title += " " + time_range

    missing = []
    lines = ["**报告名称：** %s" % title]
    if dashboard_name:
        lines.append("**仪表板：** %s" % dashboard_name)
    else:
        missing.append("仪表板")
    if time_range:
        lines.append("**查询时间：** %s" % time_range)
    else:
        missing.append("查询时间")
    if workspace_name:
        lines.append("**工作空间：** %s" % workspace_name)
    else:
        missing.append("工作空间")
    if node_info:
        lines.append("**站点：** %s" % node_info)
    else:
        missing.append("站点")
    lines.append("**报告内容：** %s" % content)
    if missing:
        lines.append("**数据说明：** 未获取到完整报告字段：%s。请检查上游 Webhook payload。" % "、".join(missing))

    elements: List[Dict[str, Any]] = [
        {"tag": "markdown", "content": "\n".join(lines)},
        {
            "tag": "img",
            "img_key": image_key,
            "alt": {"tag": "plain_text", "content": title or "报告截图"},
            "mode": "fit_horizontal",
        },
    ]
    if link_url:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看分享"},
                    "url": link_url,
                    "type": "primary",
                }
            ],
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": compact(header_title, 120)},
        },
        "elements": elements,
    }


def build_notice_card(payload: Dict[str, Any], title: str, notice: str) -> Dict[str, Any]:
    report_name = report_title(payload)
    time_range = report_time_range(payload)
    lines = [
        "**报告名称：** %s" % report_name,
    ]
    if time_range:
        lines.append("**查询时间：** %s" % time_range)
    lines.append("**处理说明：** %s" % notice)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": compact(title, 120)},
        },
        "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
    }


def send_no_image_notice(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    card = build_notice_card(
        payload,
        "【报告通知】未获取到报告截图",
        "这次通知没有带可展示的报告截图，请稍后重试或检查报告配置。",
    )
    return send_robot_message(cfg, "interactive", card)


def process_report_webhook(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = config()
    payload = normalize_payload(raw)
    verify_bridge_secret(cfg, payload)

    if cfg.get("debug_payload"):
        log_info("payload.received", payload=payload)
    else:
        log_info("payload.received", keys=list(payload.keys()) if isinstance(payload, dict) else [])

    image_urls = find_image_urls(payload, cfg.get("image_url_paths") or [])
    log_info("image_url.scan", count=len(image_urls), candidates=image_urls[:5])

    if not image_urls:
        result = {"handled": False, "reason": "image_url_not_found", "image_url_candidates": []}
        if cfg.get("send_text_when_no_image"):
            result["notice"] = send_no_image_notice(cfg, payload)
        return result

    last_error = None
    for candidate in image_urls:
        try:
            image_bytes, filename, content_type = download_image(
                candidate["url"],
                cfg.get("download_timeout_seconds", 20),
                cfg.get("max_image_bytes", 10 * 1024 * 1024),
            )
            log_info("image.downloaded", path=candidate["path"], filename=filename, content_type=content_type, bytes=len(image_bytes))
            token = get_tenant_access_token(cfg) if not cfg.get("dry_run") else "tenant_access_token_dryrun"
            image_key = upload_image(cfg, token, image_bytes, filename, content_type)
            cleanup = remember_image_key(cfg, image_key, {
                "title": report_title(payload),
                "image_url_path": candidate["path"],
                "image_bytes": len(image_bytes),
            })
            if cfg.get("message_mode") == "image":
                robot_resp = send_robot_message(cfg, "image", {"image_key": image_key})
                sent_type = "image"
            else:
                card = build_report_card(payload, image_key)
                robot_resp = send_robot_message(cfg, "interactive", card)
                sent_type = "interactive"
            log_info("image.sent", path=candidate["path"], image_key=image_key)
            return {
                "handled": True,
                "message_type": sent_type,
                "image_url_path": candidate["path"],
                "image_url_source": candidate["source"],
                "image_bytes": len(image_bytes),
                "image_key": image_key,
                "cleanup": cleanup,
                "robot": robot_resp,
            }
        except Exception as exc:
            last_error = str(exc)
            log_info("candidate.failed", path=candidate.get("path"), error=last_error)

    try:
        notice = send_robot_message(cfg, "interactive", build_notice_card(
            payload,
            "【报告通知】报告截图处理失败",
            "报告已收到，但截图暂时无法展示，请稍后重试或联系管理员查看处理日志。",
        ))
    except Exception as notice_exc:
        log_info("failure_notice.failed", error=str(notice_exc))
        notice = {"error": str(notice_exc)}
    raise BridgeError("all image URL candidates failed; last_error=%s; notice=%s" % (last_error, notice))


@DFF.API("report-feishu-image-bridge", timeout=60)
def report_feishu_image_bridge(**kwargs):
    try:
        return process_report_webhook(kwargs)
    except Exception as exc:
        log_info("bridge.error", error=str(exc))
        return {"handled": False, "error": str(exc)}
