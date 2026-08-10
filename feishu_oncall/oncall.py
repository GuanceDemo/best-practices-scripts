from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import error, parse, request

# Reusable Func template for Feishu group mention -> Guance Incident Center OnCall.
# Configure customer-specific webhook URLs, bot names, chat allowlists, and account
# mappings through Func environment variables. Do not hardcode them in this file.

try:
    DFF
except NameError:
    class _LocalDFF:
        @staticmethod
        def API(_name, **_kwargs):
            def decorator(func):
                return func
            return decorator

        @staticmethod
        def ENV(name, default=None):
            return os.environ.get(name, default)

    DFF = _LocalDFF()


MENTION_RE = re.compile(r"@[\w.\-\u4e00-\u9fff]+")
WHITESPACE_RE = re.compile(r"\s+")


DEFAULT_WEBHOOK_URL = ""
DEFAULT_STATE_FILE = "/tmp/func_oncall_template_state.json"

INCIDENT_STATUS_LABELS = {
    "creating": "故障创建中",
    "open": "待分配",
    "working": "处理中",
    "resolved": "已解决",
    "closed": "已解决",
}
INCIDENT_STATUS_TEMPLATES = {
    "creating": "blue",
    "open": "red",
    "working": "orange",
    "resolved": "green",
    "closed": "green",
}
INCIDENT_LEVEL_LABELS = {
    "system_level_0": "P0",
    "system_level_1": "P1",
    "system_level_2": "P2",
    "system_level_3": "P3",
    "system_level_4": "P4",
}
ONCALL_LEVEL_LABELS = {
    0: "一线",
    1: "二线",
    2: "三线",
}


class OncallFuncError(Exception):
    pass


def env_raw(name: str, default: Any = None) -> Any:
    value = DFF.ENV(name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None:
        return default
    return value


def env(name: str, default: str = "") -> str:
    value = env_raw(name, default)
    if value is None:
        return default
    return str(value)


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name, "true" if default else "false").strip().lower()
    return value in ("1", "true", "yes", "y", "on")


def env_json(name: str, default: Any) -> Any:
    raw = env_raw(name, None)
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    raw = str(raw)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        if isinstance(default, list):
            return [item.strip() for item in raw.split(",") if item.strip()]
        return default


def log_info(step: str, **fields: Any) -> None:
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key not in ("df_api_key", "api_key", "token", "verify_token", "app_secret")
    }
    print("[oncall] " + json.dumps({"step": step, **safe_fields}, ensure_ascii=False, default=str))


def config() -> Dict[str, Any]:
    return {
        "dry_run": env_bool("ONCALL_DRY_RUN", False),
        "lark": {
            "verify_token": env("LARK_VERIFY_TOKEN", ""),
            "target_chat_ids": env_json("ONCALL_TARGET_CHAT_IDS", []),
            "known_chat_names": env_json("ONCALL_KNOWN_CHAT_NAMES", {}),
            "known_user_names": env_json("ONCALL_KNOWN_USER_NAMES", {}),
            "require_group": env_bool("ONCALL_REQUIRE_GROUP", True),
            "require_bot_mention": env_bool("ONCALL_REQUIRE_BOT_MENTION", True),
            "trigger_bot_names": env_json("ONCALL_TRIGGER_BOT_NAMES", []),
            "allowed_message_types": env_json("ONCALL_ALLOWED_MESSAGE_TYPES", ["text", "post"]),
        },
        "guance": {
            "base_url": env("GUANCE_OPENAPI_BASE_URL", "https://openapi.guance.com").rstrip("/"),
            "webhook_url": env("GUANCE_WEBHOOK_URL", DEFAULT_WEBHOOK_URL),
            "df_api_key": env("DF_API_KEY", ""),
            "timeout_seconds": int(env("GUANCE_TIMEOUT_SECONDS", "10") or "10"),
            "retry_count": int(env("GUANCE_RETRY_COUNT", "1") or "1"),
            "incident_resolve_seconds": int(env("GUANCE_INCIDENT_RESOLVE_SECONDS", "12") or "12"),
        },
        "feishu": {
            "app_id": env("LARK_APP_ID", ""),
            "app_secret": env("LARK_APP_SECRET", ""),
            "base_url": env("LARK_OPENAPI_BASE_URL", "https://open.feishu.cn"),
            "send_card": env_bool("ONCALL_SEND_CARD", True),
            "today_oncall": env("ONCALL_TODAY_ONCALL", "OnCall 值班策略"),
        },
        "oncall": {
            "schedule_uuid": env("ONCALL_SCHEDULE_UUID", ""),
            "schedule_name": env("ONCALL_SCHEDULE_NAME", "oncall"),
            "fallback_display": env("ONCALL_TODAY_ONCALL", "OnCall 值班策略"),
            "account_map": env_json("ONCALL_GUANCE_ACCOUNT_MAP", {}),
            "strict_card_permission": env_bool("ONCALL_STRICT_CARD_PERMISSION", False),
            "card_update_multi": env_bool("ONCALL_CARD_UPDATE_MULTI", True),
            "state_file": env("ONCALL_STATE_FILE", DEFAULT_STATE_FILE),
            "level_displays": env_json("ONCALL_LEVEL_DISPLAYS", {}),
            "level1_display": env("ONCALL_LEVEL1_DISPLAY", ""),
            "level2_display": env("ONCALL_LEVEL2_DISPLAY", ""),
            "level3_display": env("ONCALL_LEVEL3_DISPLAY", ""),
        },
        "event": {
            "status": env("ONCALL_EVENT_STATUS", "fatal"),
            "title_prefix": env("ONCALL_TITLE_PREFIX", "P0Oncall"),
            "source": env("ONCALL_SOURCE", "feishu_oncall"),
            "service": env("ONCALL_SERVICE", "oncall"),
            "check_value": float(env("ONCALL_CHECK_VALUE", "1") or "1"),
        },
    }


def compact_text(text: Any, limit: int = 140) -> str:
    value = WHITESPACE_RE.sub(" ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def strip_mentions(text: str, bot_names: Iterable[str]) -> str:
    value = str(text or "")
    if isinstance(bot_names, str):
        bot_names = [bot_names]
    names = [str(name).strip() for name in bot_names or [] if str(name).strip()]
    for name in names:
        name_pattern = re.escape(name).replace(r"\ ", r"\s+")
        value = re.sub(r"@" + name_pattern + r"\b", " ", value)
    return compact_text(value, 4000)


def short_id(value: Any, prefix: int = 6, suffix: int = 6) -> str:
    text = str(value or "")
    if len(text) <= prefix + suffix + 3:
        return text
    return text[:prefix] + "..." + text[-suffix:]


def readable_fallback(label: str, value: Any) -> str:
    value = str(value or "")
    if not value:
        return "未知" + label
    return "未知%s（%s）" % (label, short_id(value))


def mapped_name(cfg: Dict[str, Any], map_name: str, object_id: str) -> Optional[str]:
    mapping = (cfg.get("lark") or {}).get(map_name) or {}
    if isinstance(mapping, dict):
        value = mapping.get(object_id)
        if value:
            return str(value)
    return None


def event_chat_name(cfg: Dict[str, Any], lark_event: Dict[str, Any]) -> str:
    chat_id = str(lark_event.get("chat_id") or "")
    return (
        str(lark_event.get("chat_name") or "")
        or mapped_name(cfg, "known_chat_names", chat_id)
        or readable_fallback("群", chat_id)
    )


def event_sender_name(cfg: Dict[str, Any], lark_event: Dict[str, Any]) -> str:
    sender_id = str(lark_event.get("sender_id") or "")
    return (
        str(lark_event.get("sender_name") or "")
        or mapped_name(cfg, "known_user_names", sender_id)
        or readable_fallback("用户", sender_id)
    )


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def event_epoch_seconds(lark_event: Dict[str, Any]) -> int:
    create_ms = safe_int(lark_event.get("create_time") or lark_event.get("timestamp"), 0)
    if create_ms > 0:
        return create_ms // 1000
    return int(time.time())


def event_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def oncall_event_id(lark_event: Dict[str, Any]) -> str:
    raw = lark_event.get("message_id") or lark_event.get("id") or lark_event.get("event_id")
    if raw:
        return "ONCALL-" + event_hash(str(raw)).upper()
    return "ONCALL-" + uuid.uuid4().hex[:12].upper()


def bot_mentioned(cfg: Dict[str, Any], lark_event: Dict[str, Any]) -> bool:
    lark_cfg = cfg.get("lark") or {}
    names = [str(name).strip() for name in (lark_cfg.get("trigger_bot_names") or []) if str(name).strip()]
    app_id = str((cfg.get("feishu") or {}).get("app_id") or "")
    mentions = lark_event.get("mentions") if isinstance(lark_event.get("mentions"), list) else []

    for item in mentions:
        if not isinstance(item, dict):
            continue
        mention_name = str(item.get("name") or "")
        mention_id = str(item.get("id") or "")
        if mention_name in names or mention_name.lower() in [name.lower() for name in names]:
            return True
        if app_id and mention_id == app_id:
            return True

    content = str(lark_event.get("content") or "")
    normalized_content = WHITESPACE_RE.sub(" ", content).strip().lower()
    for name in names:
        normalized_name = WHITESPACE_RE.sub(" ", name).strip().lower()
        if normalized_name and ("@" + normalized_name) in normalized_content:
            return True
    return False


def should_handle_event(cfg: Dict[str, Any], lark_event: Dict[str, Any]) -> Tuple[bool, str]:
    lark_cfg = cfg.get("lark") or {}

    if lark_cfg.get("require_group", True) and lark_event.get("chat_type") != "group":
        return False, "not_group_chat"

    allowed_types = set(lark_cfg.get("allowed_message_types") or [])
    if allowed_types and lark_event.get("message_type") not in allowed_types:
        return False, "message_type_not_allowed"

    target_chat_ids = set(lark_cfg.get("target_chat_ids") or [])
    if target_chat_ids and lark_event.get("chat_id") not in target_chat_ids:
        return False, "chat_id_not_targeted"

    if lark_cfg.get("require_bot_mention"):
        if not bot_mentioned(cfg, lark_event):
            return False, "bot_mention_not_found"

    return True, "ok"


def build_guance_payload(cfg: Dict[str, Any], lark_event: Dict[str, Any]) -> Dict[str, Any]:
    event_cfg = cfg.get("event") or {}
    lark_cfg = cfg.get("lark") or {}

    event_id = oncall_event_id(lark_event)
    raw_content = str(lark_event.get("content") or "")
    message_text = strip_mentions(raw_content, lark_cfg.get("trigger_bot_names") or [])
    if not message_text:
        message_text = raw_content.strip() or "(empty message)"

    chat_id = str(lark_event.get("chat_id") or "")
    message_id = str(lark_event.get("message_id") or lark_event.get("id") or "")
    sender_id = str(lark_event.get("sender_id") or "")
    chat_name = event_chat_name(cfg, lark_event)
    sender_name = event_sender_name(cfg, lark_event)
    message_link = str(lark_event.get("message_app_link") or "")
    created_at = event_epoch_seconds(lark_event)
    title = "%s: %s" % (event_cfg.get("title_prefix") or "P0Oncall", compact_text(message_text, 60))

    dimension_tags = {
        "source": str(event_cfg.get("source") or "lark_oncall"),
        "service": str(event_cfg.get("service") or "oncall_test"),
        "lark_chat_name": chat_name,
        "lark_sender_name": sender_name,
        "lark_chat_id": chat_id,
        "lark_message_id": message_id,
        "lark_sender_id": sender_id,
        "lark_oncall_id": event_id,
    }

    event_body = {
        "date": created_at,
        "status": str(event_cfg.get("status") or "critical"),
        "title": title,
        "message": "\n".join(
            [
                "提报人：%s" % sender_name,
                "群聊：%s" % chat_name,
                "反馈内容：%s" % message_text,
                "消息链接：%s" % (message_link or "暂无"),
                "",
                "关联ID：%s" % event_id,
                "排查字段：",
                "- chat_id: %s" % (chat_id or "unknown"),
                "- message_id: %s" % (message_id or "unknown"),
                "- sender_id: %s" % (sender_id or "unknown"),
            ]
        ),
        "dimension_tags": dimension_tags,
        "check_value": float(event_cfg.get("check_value", 1)),
        "biz_lark_oncall_id": event_id,
        "biz_lark_message_id": message_id,
        "biz_lark_chat_id": chat_id,
    }

    extra_data = {
        "lark_oncall_id": event_id,
        "lark_event_id": lark_event.get("event_id"),
        "lark_chat_id": chat_id,
        "lark_chat_name": chat_name,
        "lark_chat_type": lark_event.get("chat_type"),
        "lark_message_id": message_id,
        "lark_message_link": message_link,
        "lark_message_type": lark_event.get("message_type"),
        "lark_sender_id": sender_id,
        "lark_sender_name": sender_name,
        "lark_create_time": lark_event.get("create_time"),
        "feedback_content": message_text,
        "raw_lark_event": lark_event,
    }
    return {"event": event_body, "extraData": extra_data}


def send_http(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    if cfg.get("dry_run"):
        log_info("guance.dry_run", title=(payload.get("event") or {}).get("title"))
        return {"transport": "dry_run", "response": payload}

    guance_cfg = cfg.get("guance") or {}
    url = guance_cfg.get("webhook_url")
    if not url:
        raise OncallFuncError("GUANCE_WEBHOOK_URL is required")

    api_key = guance_cfg.get("df_api_key")
    if not api_key:
        raise OncallFuncError("DF_API_KEY is required")

    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "DF-API-KEY": api_key,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timeout = int(guance_cfg.get("timeout_seconds") or 10)
    retry_count = max(1, int(guance_cfg.get("retry_count") or 1))
    last_error = None

    for attempt in range(1, retry_count + 1):
        log_info(
            "guance.push.start",
            attempt=attempt,
            retry_count=retry_count,
            timeout_seconds=timeout,
            title=(payload.get("event") or {}).get("title"),
            lark_oncall_id=(payload.get("event") or {}).get("dimension_tags", {}).get("lark_oncall_id"),
        )
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            log_info("guance.push.http_error", attempt=attempt, status=exc.code, reason=exc.reason, body=raw[:500])
            raise OncallFuncError("Guance HTTP %s %s: %s" % (exc.code, exc.reason, raw))
        except Exception as exc:
            last_error = exc
            log_info("guance.push.error", attempt=attempt, error=str(exc))
            if attempt < retry_count:
                time.sleep(min(1.5, 0.5 * attempt))
                continue
            raise OncallFuncError("Guance HTTP failed after %s attempt(s): %s" % (retry_count, last_error))

        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw}
        trace_id = parsed.get("traceId") if isinstance(parsed, dict) else ""
        log_info("guance.push.success", attempt=attempt, status=status, trace_id=trace_id)
        return {"transport": "http", "status": status, "response": parsed}

    raise OncallFuncError("Guance HTTP failed: %s" % last_error)


def request_json(
    url: str,
    method: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {"raw": raw}


def post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int = 10) -> Dict[str, Any]:
    return request_json(url, "POST", headers, payload=payload, timeout=timeout)


def get_json(url: str, headers: Dict[str, str], timeout: int = 10) -> Dict[str, Any]:
    return request_json(url, "GET", headers, payload=None, timeout=timeout)


def guance_base_url(cfg: Dict[str, Any]) -> str:
    return ((cfg.get("guance") or {}).get("base_url") or "https://openapi.guance.com").rstrip("/") + "/api/v1"


def guance_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    guance_cfg = cfg.get("guance") or {}
    api_key = guance_cfg.get("df_api_key")
    if not api_key:
        raise OncallFuncError("DF_API_KEY is required")
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "DF-API-KEY": api_key,
    }


def ensure_guance_response(path: str, resp: Dict[str, Any]) -> Dict[str, Any]:
    if resp.get("code") not in (None, 200):
        raise OncallFuncError("Guance API %s failed: %s" % (path, resp))
    return resp


def guance_api_post(cfg: Dict[str, Any], path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    guance_cfg = cfg.get("guance") or {}
    url = guance_base_url(cfg) + path
    resp = post_json(url, payload, guance_headers(cfg), timeout=int(guance_cfg.get("timeout_seconds") or 10))
    return ensure_guance_response(path, resp)


def guance_api_get(cfg: Dict[str, Any], path: str, query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    guance_cfg = cfg.get("guance") or {}
    url = guance_base_url(cfg) + path
    if query:
        url += "?" + parse.urlencode(query, doseq=True)
    resp = get_json(url, guance_headers(cfg), timeout=int(guance_cfg.get("timeout_seconds") or 10))
    return ensure_guance_response(path, resp)


def response_data(resp: Dict[str, Any]) -> Any:
    content = resp.get("content") if isinstance(resp.get("content"), dict) else {}
    return content.get("data")


def response_content(resp: Dict[str, Any]) -> Any:
    return resp.get("content") if isinstance(resp.get("content"), dict) else resp


def unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value.get("data")
    return value


def unwrap_item(value: Any) -> Dict[str, Any]:
    data = unwrap_data(value)
    if isinstance(data, dict):
        item = data.get("item")
        if isinstance(item, dict):
            return item
        if "items" not in data:
            return data
    return {}


def unwrap_items(value: Any) -> List[Dict[str, Any]]:
    data = unwrap_data(value)
    if isinstance(data, dict):
        items = data.get("items")
    elif isinstance(data, list):
        items = data
    else:
        items = []
    return [item for item in items or [] if isinstance(item, dict)]


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def distinct(values: Iterable[Any]) -> List[str]:
    seen = set()
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def state_file_path(cfg: Dict[str, Any]) -> str:
    return str(((cfg.get("oncall") or {}).get("state_file") or DEFAULT_STATE_FILE))


def empty_state() -> Dict[str, Any]:
    return {
        "cards_by_incident_uuid": {},
        "cards_by_lark_oncall_id": {},
        "cards_by_event_ref": {},
    }


def load_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = state_file_path(cfg)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            for key, default_value in empty_state().items():
                if not isinstance(state.get(key), dict):
                    state[key] = default_value
            return state
    except FileNotFoundError:
        return empty_state()
    except Exception as exc:
        log_info("state.load.failed", path=path, error=str(exc))
    return empty_state()


def save_state(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    path = state_file_path(cfg)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, default=str)
        os.replace(tmp_path, path)
    except Exception as exc:
        log_info("state.save.failed", path=path, error=str(exc))


def remember_card_mapping(
    cfg: Dict[str, Any],
    payload: Dict[str, Any],
    resolved: Dict[str, Any],
    message_id: str,
    chat_id: str,
) -> None:
    event_body = payload.get("event") or {}
    tags = event_body.get("dimension_tags") or {}
    incident = resolved.get("incident") or {}
    event_item = resolved.get("event") or {}
    lark_oncall_id = str(tags.get("lark_oncall_id") or "")
    incident_uuid = str(incident.get("incident_uuid") or incident.get("uuid") or "")
    event_ref = str(event_item.get("event_ref") or incident.get("resource_identity") or "")
    if not message_id:
        return

    entry = {
        "message_id": message_id,
        "chat_id": chat_id or tags.get("lark_chat_id") or "",
        "incident_uuid": incident_uuid,
        "lark_oncall_id": lark_oncall_id,
        "event_ref": event_ref,
        "payload": payload,
        "resolved": resolved,
        "updated_at": int(time.time()),
    }
    state = load_state(cfg)
    if incident_uuid:
        state.setdefault("cards_by_incident_uuid", {})[incident_uuid] = entry
    if lark_oncall_id:
        state.setdefault("cards_by_lark_oncall_id", {})[lark_oncall_id] = entry
    if event_ref:
        state.setdefault("cards_by_event_ref", {})[event_ref] = entry
    save_state(cfg, state)
    log_info(
        "state.card.remembered",
        incident_uuid=incident_uuid,
        lark_oncall_id=lark_oncall_id,
        event_ref=event_ref,
        message_id=message_id,
    )


def lookup_card_mapping(
    cfg: Dict[str, Any],
    incident_uuid: str = "",
    lark_oncall_id: str = "",
    event_ref: str = "",
) -> Dict[str, Any]:
    state = load_state(cfg)
    lookups = [
        ("cards_by_incident_uuid", incident_uuid),
        ("cards_by_lark_oncall_id", lark_oncall_id),
        ("cards_by_event_ref", event_ref),
    ]
    for bucket, key in lookups:
        if not key:
            continue
        entry = (state.get(bucket) or {}).get(str(key))
        if isinstance(entry, dict):
            return entry
    return {}


def format_time(value: Any, empty: str = "待确认") -> str:
    timestamp = safe_int(value, 0)
    if timestamp <= 0:
        return empty
    if timestamp > 10_000_000_000:
        timestamp = timestamp // 1000
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except Exception:
        return empty


def status_label(status: Any) -> str:
    return INCIDENT_STATUS_LABELS.get(str(status or "").lower(), str(status or "待分配"))


def status_template(status: Any) -> str:
    return INCIDENT_STATUS_TEMPLATES.get(str(status or "").lower(), "red")


def level_label(level: Any) -> str:
    value = str(level or "")
    if not value:
        return "P0"
    if value in INCIDENT_LEVEL_LABELS:
        return INCIDENT_LEVEL_LABELS[value]
    if value.upper().startswith("P"):
        return value.upper()
    return "P0"


def query_guance_event_once(cfg: Dict[str, Any], lark_oncall_id: str, since_ms: int) -> Optional[Dict[str, Any]]:
    if not lark_oncall_id:
        return None
    start_ms = max(0, since_ms - 5 * 60 * 1000)
    end_ms = int(time.time() * 1000) + 60 * 1000
    resp = guance_api_post(
        cfg,
        "/events/abnormal/list",
        {
            "offset": 0,
            "limit": 50,
            "timeRange": [start_ms, end_ms],
            "lastStatus": "fatal",
            "search": lark_oncall_id,
        },
    )
    for item in response_data(resp) or []:
        tags = parse_json_object(item.get("df_dimension_tags"))
        if tags.get("lark_oncall_id") == lark_oncall_id:
            event_ref = item.get("df_monitor_checker_event_ref")
            if event_ref:
                return {
                    "doc_id": item.get("__docid"),
                    "event_ref": event_ref,
                    "title": item.get("df_title"),
                    "dimension_tags": tags,
                }
    return None


def query_guance_event_by_ref(cfg: Dict[str, Any], event_ref: str, since_ms: int = 0) -> Optional[Dict[str, Any]]:
    if not event_ref:
        return None
    start_ms = max(0, (since_ms or int(time.time() * 1000)) - 24 * 60 * 60 * 1000)
    end_ms = int(time.time() * 1000) + 60 * 1000
    try:
        resp = guance_api_post(
            cfg,
            "/events/abnormal/list",
            {
                "offset": 0,
                "limit": 100,
                "timeRange": [start_ms, end_ms],
                "search": event_ref,
            },
        )
    except Exception as exc:
        log_info("event.lookup_by_ref.failed", event_ref=event_ref, error=str(exc))
        return None
    for item in response_data(resp) or []:
        if item.get("df_monitor_checker_event_ref") != event_ref:
            continue
        tags = parse_json_object(item.get("df_dimension_tags"))
        return {
            "doc_id": item.get("__docid"),
            "event_ref": event_ref,
            "title": item.get("df_title"),
            "message": item.get("df_message"),
            "dimension_tags": tags,
            "date": item.get("df_date") or item.get("date") or item.get("time"),
        }
    return None


def find_guance_event(cfg: Dict[str, Any], lark_oncall_id: str, since_ms: int, timeout_seconds: int) -> Optional[Dict[str, Any]]:
    deadline = time.time() + max(1, timeout_seconds)
    while True:
        event_item = query_guance_event_once(cfg, lark_oncall_id, since_ms)
        if event_item:
            return event_item
        if time.time() >= deadline:
            return None
        time.sleep(2)


def normalize_incident(item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    incident_uuid = item.get("uuid") or item.get("incident_uuid")
    return {
        "id": item.get("id"),
        "incident_uuid": incident_uuid,
        "uuid": incident_uuid,
        "name": item.get("name"),
        "incidents_status": item.get("incidentsStatus") or item.get("incidents_status"),
        "level": item.get("level") or item.get("incidentsLevel"),
        "resource_identity": item.get("resourceIdentity") or item.get("resource_identity"),
        "assigner": item.get("assigner") or [],
        "assigner_info": item.get("assignerInfo") or item.get("assigner_info") or [],
        "schedule_info": item.get("scheduleInfo") or item.get("schedule_info") or {},
        "create_at": item.get("createAt") or item.get("create_at") or item.get("createTime"),
        "update_at": item.get("updateAt") or item.get("update_at"),
        "status_time": item.get("statusTime") or item.get("status_time") or {},
    }


def get_incident_detail(cfg: Dict[str, Any], incident_uuid: str) -> Dict[str, Any]:
    if not incident_uuid:
        return {}
    try:
        resp = guance_api_get(cfg, "/incidents/%s/get" % parse.quote(str(incident_uuid), safe=""))
        detail = unwrap_item(response_content(resp)) or unwrap_item(resp)
        normalized = normalize_incident(detail)
        if normalized:
            return normalized
    except Exception as exc:
        log_info("incident.get.failed", incident_uuid=incident_uuid, error=str(exc))
    return {}


def get_incident_operations(cfg: Dict[str, Any], incident_uuid: str) -> List[Dict[str, Any]]:
    if not incident_uuid:
        return []
    try:
        resp = guance_api_get(
            cfg,
            "/incidents/operate/%s/list" % parse.quote(str(incident_uuid), safe=""),
            {"pageIndex": 1, "pageSize": 50},
        )
        return unwrap_items(response_content(resp)) or unwrap_items(resp)
    except Exception as exc:
        log_info("incident.operations.failed", incident_uuid=incident_uuid, error=str(exc))
        return []


def incident_escalated_level(cfg: Dict[str, Any], incident_uuid: str) -> int:
    level = 0
    for item in get_incident_operations(cfg, incident_uuid):
        operate_type = item.get("operate_type") or item.get("operateType")
        if operate_type != "notify_level_update":
            continue
        content = str(item.get("content") or "")
        extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
        strategy_info = extend.get("strategy_info") if isinstance(extend.get("strategy_info"), dict) else {}
        strategy = strategy_info.get("strategy") if isinstance(strategy_info.get("strategy"), dict) else {}
        level = max(level, safe_int(strategy.get("level"), 0))
        for match in re.findall(r"Level\s*(\d+)", content, flags=re.IGNORECASE):
            level = max(level, safe_int(match, 0))
    return level


def merge_incident(base: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in (detail or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def find_incident(cfg: Dict[str, Any], event_ref: str, title: str, timeout_seconds: int) -> Optional[Dict[str, Any]]:
    deadline = time.time() + max(1, timeout_seconds)
    search_text = compact_text(title or "P0Oncall", 60)
    while True:
        resp = guance_api_post(
            cfg,
            "/incidents/list",
            {
                "pageIndex": 1,
                "pageSize": 50,
                "search": search_text,
            },
        )
        for item in response_data(resp) or []:
            if item.get("resourceIdentity") == event_ref:
                incident = normalize_incident(item)
                return merge_incident(incident, get_incident_detail(cfg, incident.get("incident_uuid") or ""))
        if time.time() >= deadline:
            return None
        time.sleep(2)


def resolve_incident(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    event_body = payload.get("event") or {}
    tags = event_body.get("dimension_tags") or {}
    lark_oncall_id = tags.get("lark_oncall_id")
    since_ms = int(event_body.get("date") or time.time()) * 1000
    timeout_seconds = int((cfg.get("guance") or {}).get("incident_resolve_seconds") or 12)
    log_info("incident.resolve.start", lark_oncall_id=lark_oncall_id, timeout_seconds=timeout_seconds)
    event_item = find_guance_event(cfg, lark_oncall_id, since_ms, timeout_seconds)
    if not event_item:
        log_info("incident.resolve.event_not_found", lark_oncall_id=lark_oncall_id)
        return {"resolved": False, "reason": "event_not_found"}
    incident = find_incident(cfg, event_item["event_ref"], event_body.get("title") or "P0Oncall", timeout_seconds)
    if not incident:
        log_info("incident.resolve.incident_not_found", event_ref=event_item["event_ref"], doc_id=event_item.get("doc_id"))
        return {"resolved": False, "reason": "incident_not_found", "event": event_item}
    log_info(
        "incident.resolve.success",
        doc_id=event_item.get("doc_id"),
        event_ref=event_item.get("event_ref"),
        incident_uuid=incident.get("incident_uuid"),
        incidents_status=incident.get("incidents_status"),
    )
    return {"resolved": True, "event": event_item, "incident": incident}


def feishu_tenant_access_token(cfg: Dict[str, Any]) -> str:
    feishu_cfg = cfg.get("feishu") or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")
    if not app_id or not app_secret:
        raise OncallFuncError("LARK_APP_ID and LARK_APP_SECRET are required for card sending")
    url = feishu_cfg.get("base_url", "https://open.feishu.cn").rstrip("/") + "/open-apis/auth/v3/tenant_access_token/internal"
    resp = post_json(
        url,
        {"app_id": app_id, "app_secret": app_secret},
        {"Content-Type": "application/json;charset=UTF-8"},
        timeout=10,
    )
    if resp.get("code") != 0:
        raise OncallFuncError("Feishu tenant token failed: %s" % resp)
    return str(resp.get("tenant_access_token") or "")


def account_map_entry(cfg: Dict[str, Any], account_uuid: str) -> Dict[str, Any]:
    mapping = ((cfg.get("oncall") or {}).get("account_map") or {})
    raw = mapping.get(account_uuid) if isinstance(mapping, dict) else None
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        return {"name": raw, "display_name": raw}
    return {}


def account_display_name(cfg: Dict[str, Any], account_uuid: str, member_map: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    mapped = account_map_entry(cfg, account_uuid)
    for key in ("display_name", "name", "lark_name", "guance_name"):
        if mapped.get(key):
            return str(mapped.get(key))
    member = (member_map or {}).get(account_uuid) or {}
    for key in ("name", "username", "email"):
        if member.get(key):
            return str(member.get(key))
    return short_id(account_uuid, 10, 6)


def account_lark_open_id(cfg: Dict[str, Any], account_uuid: str) -> str:
    ids = account_lark_open_ids(cfg, account_uuid)
    return ids[0] if ids else ""


def account_lark_open_ids(cfg: Dict[str, Any], account_uuid: str) -> List[str]:
    mapped = account_map_entry(cfg, account_uuid)
    values: List[Any] = []
    for key in ("lark_open_ids", "open_ids"):
        raw = mapped.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    values.append(mapped.get("lark_open_id") or mapped.get("open_id") or "")
    return distinct(values)


def account_lark_user_ids(cfg: Dict[str, Any], account_uuid: str) -> List[str]:
    mapped = account_map_entry(cfg, account_uuid)
    values: List[Any] = []
    for key in ("lark_user_ids", "user_ids"):
        raw = mapped.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    values.append(mapped.get("lark_user_id") or mapped.get("user_id") or "")
    return distinct(values)


def account_lark_union_ids(cfg: Dict[str, Any], account_uuid: str) -> List[str]:
    mapped = account_map_entry(cfg, account_uuid)
    values: List[Any] = []
    for key in ("lark_union_ids", "union_ids"):
        raw = mapped.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    values.append(mapped.get("lark_union_id") or mapped.get("union_id") or "")
    return distinct(values)


def account_lark_ids(cfg: Dict[str, Any], account_uuid: str) -> List[str]:
    return distinct(
        account_lark_open_ids(cfg, account_uuid)
        + account_lark_user_ids(cfg, account_uuid)
        + account_lark_union_ids(cfg, account_uuid)
    )


def guance_account_by_lark_open_id(cfg: Dict[str, Any], lark_open_id: str) -> str:
    mapping = ((cfg.get("oncall") or {}).get("account_map") or {})
    if not isinstance(mapping, dict):
        return ""
    for account_uuid, raw in mapping.items():
        if not isinstance(raw, dict):
            continue
        configured_ids = []
        for key in ("lark_open_ids", "open_ids", "lark_user_ids", "user_ids", "lark_union_ids", "union_ids"):
            value = raw.get(key)
            if isinstance(value, list):
                configured_ids.extend(value)
            elif value:
                configured_ids.append(value)
        configured_ids.append(raw.get("lark_open_id") or raw.get("open_id") or "")
        configured_ids.append(raw.get("lark_user_id") or raw.get("user_id") or "")
        configured_ids.append(raw.get("lark_union_id") or raw.get("union_id") or "")
        if lark_open_id in set(distinct(configured_ids)):
            return str(account_uuid)
    return ""


def fetch_workspace_members(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    for method in ("GET", "POST"):
        try:
            if method == "GET":
                resp = guance_api_get(cfg, "/workspace/members/list", {"pageIndex": 1, "pageSize": 100})
            else:
                resp = guance_api_post(cfg, "/workspace/members/list", {"pageIndex": 1, "pageSize": 200})
            items = unwrap_items(response_content(resp)) or unwrap_items(resp)
            member_map = {}
            for item in items:
                member_uuid = str(item.get("member_uuid") or item.get("uuid") or item.get("accountUUID") or "")
                if member_uuid:
                    member_map[member_uuid] = item
            if member_map:
                log_info("oncall.members.resolved", count=len(member_map), method=method)
                return member_map
        except Exception as exc:
            log_info("oncall.members.failed", method=method, error=str(exc))
    return {}


def fetch_schedule_list(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    for method in ("GET", "POST"):
        try:
            if method == "GET":
                resp = guance_api_get(cfg, "/incidents/schedule/list", {"pageIndex": 1, "pageSize": 50})
            else:
                resp = guance_api_post(cfg, "/incidents/schedule/list", {"pageIndex": 1, "pageSize": 50})
            items = unwrap_items(response_content(resp)) or unwrap_items(resp)
            if items:
                return items
        except Exception as exc:
            log_info("oncall.schedule.list_failed", method=method, error=str(exc))
    return []


def discover_schedule_uuid(cfg: Dict[str, Any]) -> str:
    oncall_cfg = cfg.get("oncall") or {}
    schedule_uuid = str(oncall_cfg.get("schedule_uuid") or "")
    if schedule_uuid:
        return schedule_uuid
    schedule_name = str(oncall_cfg.get("schedule_name") or "").strip()
    if not schedule_name:
        return ""
    for item in fetch_schedule_list(cfg):
        if str(item.get("name") or "") == schedule_name:
            return str(item.get("schedule_uuid") or item.get("uuid") or "")
    return ""


def fetch_schedule_detail(cfg: Dict[str, Any]) -> Dict[str, Any]:
    schedule_uuid = discover_schedule_uuid(cfg)
    if not schedule_uuid:
        return {}
    escaped_uuid = parse.quote(schedule_uuid, safe="")
    paths = [
        "/incidents/schedule/%s/get" % escaped_uuid,
        "/incident/schedule/%s/get" % escaped_uuid,
    ]
    for path in paths:
        for method in ("GET", "POST"):
            try:
                if method == "GET":
                    resp = guance_api_get(cfg, path)
                else:
                    resp = guance_api_post(cfg, path, {})
                item = unwrap_item(response_content(resp)) or unwrap_item(resp)
                if item:
                    log_info(
                        "oncall.schedule.resolved",
                        schedule_uuid=item.get("schedule_uuid") or schedule_uuid,
                        schedule_name=item.get("name"),
                        method=method,
                    )
                    return item
            except Exception as exc:
                log_info("oncall.schedule.get_failed", path=path, method=method, error=str(exc))
    return {}


def add_roster_entry(
    entries: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    member_map: Dict[str, Dict[str, Any]],
    level_index: int,
    account_uuid: str,
) -> None:
    account_uuid = str(account_uuid or "").strip()
    if not account_uuid or account_uuid == "watchkeeper":
        return
    key = (level_index, account_uuid)
    for entry in entries:
        if (entry.get("level_index"), entry.get("guance_account_uuid")) == key:
            return
    lark_open_id = account_lark_open_id(cfg, account_uuid)
    lark_open_ids = account_lark_open_ids(cfg, account_uuid)
    lark_user_ids = account_lark_user_ids(cfg, account_uuid)
    lark_union_ids = account_lark_union_ids(cfg, account_uuid)
    entries.append(
        {
            "level_index": level_index,
            "level_label": ONCALL_LEVEL_LABELS.get(level_index, "L%s" % (level_index + 1)),
            "guance_account_uuid": account_uuid,
            "lark_open_id": lark_open_id,
            "lark_open_ids": lark_open_ids,
            "lark_user_ids": lark_user_ids,
            "lark_union_ids": lark_union_ids,
            "lark_ids": distinct(lark_open_ids + lark_user_ids + lark_union_ids),
            "display_name": account_display_name(cfg, account_uuid, member_map),
        }
    )


def iter_strategy_items(strategy_config: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(strategy_config, list):
        for group in strategy_config:
            if isinstance(group, dict):
                for item in group.get("strategy") or []:
                    if isinstance(item, dict):
                        yield item
    elif isinstance(strategy_config, dict):
        for item in strategy_config.get("strategy") or []:
            if isinstance(item, dict):
                yield item


def resolve_oncall_roster(cfg: Dict[str, Any], resolved: Dict[str, Any]) -> Dict[str, Any]:
    oncall_cfg = cfg.get("oncall") or {}
    member_map = fetch_workspace_members(cfg)
    schedule = fetch_schedule_detail(cfg)
    entries: List[Dict[str, Any]] = []

    notify_targets = schedule.get("notify_targets") or schedule.get("notifyTargets") or []
    for account_uuid in notify_targets:
        add_roster_entry(entries, cfg, member_map, 0, account_uuid)

    for item in iter_strategy_items(schedule.get("strategy_config") or schedule.get("strategyConfig")):
        level_index = safe_int(item.get("level"), 0)
        for notify in item.get("notifyConfig") or item.get("notify_config") or []:
            if not isinstance(notify, dict):
                continue
            if notify.get("notifyObjectType") not in ("member", None):
                continue
            notify_objects = notify.get("notifyObject") or notify.get("notify_object") or []
            if "watchkeeper" in notify_objects:
                for account_uuid in notify_targets:
                    add_roster_entry(entries, cfg, member_map, level_index, account_uuid)
                continue
            for account_uuid in notify_objects:
                add_roster_entry(entries, cfg, member_map, level_index, account_uuid)

    incident = resolved.get("incident") or {}
    if not entries:
        for account_uuid in incident.get("assigner") or []:
            add_roster_entry(entries, cfg, member_map, 0, account_uuid)

    escalated_level = incident_escalated_level(cfg, str(incident.get("incident_uuid") or ""))

    if not entries:
        fallback_display = str(oncall_cfg.get("fallback_display") or (cfg.get("feishu") or {}).get("today_oncall") or "")
        return {
            "source": "fallback",
            "schedule_uuid": schedule.get("schedule_uuid") or schedule.get("scheduleUUID") or oncall_cfg.get("schedule_uuid") or "",
            "current_display": fallback_display or "故障中心通知策略",
            "all_display": fallback_display or "故障中心通知策略",
            "escalated_level": escalated_level,
            "current_members": [],
            "all_members": [],
            "allowed_guance_accounts": [],
            "allowed_lark_open_ids": [],
            "allowed_lark_user_ids": [],
            "allowed_lark_union_ids": [],
            "allowed_lark_ids": [],
        }

    visible_level = max(0, escalated_level)
    current_members = [entry for entry in entries if safe_int(entry.get("level_index"), 0) <= visible_level]
    return {
        "source": "guance_schedule" if schedule else "incident_assigner",
        "schedule_uuid": schedule.get("schedule_uuid") or schedule.get("scheduleUUID") or oncall_cfg.get("schedule_uuid") or "",
        "schedule_name": schedule.get("name") or oncall_cfg.get("schedule_name") or "",
        "current_display": format_roster_entries(current_members, cfg),
        "all_display": format_roster_entries(entries, cfg),
        "escalated_level": escalated_level,
        "current_members": current_members,
        "all_members": entries,
        "allowed_guance_accounts": distinct([entry.get("guance_account_uuid") for entry in current_members]),
        "allowed_lark_open_ids": distinct(
            open_id
            for entry in current_members
            for open_id in ((entry.get("lark_open_ids") or []) + ([entry.get("lark_open_id")] if entry.get("lark_open_id") else []))
        ),
        "allowed_lark_user_ids": distinct(
            user_id
            for entry in current_members
            for user_id in (entry.get("lark_user_ids") or [])
        ),
        "allowed_lark_union_ids": distinct(
            union_id
            for entry in current_members
            for union_id in (entry.get("lark_union_ids") or [])
        ),
        "allowed_lark_ids": distinct(
            lark_id
            for entry in current_members
            for lark_id in (entry.get("lark_ids") or [])
        ),
    }


def configured_level_display(cfg: Optional[Dict[str, Any]], level_index: int) -> str:
    if not cfg:
        return ""
    oncall_cfg = cfg.get("oncall") or {}
    direct_key = "level%s_display" % (level_index + 1)
    direct_value = str(oncall_cfg.get(direct_key) or "").strip()
    if direct_value:
        return direct_value
    display_map = oncall_cfg.get("level_displays") or {}
    if not isinstance(display_map, dict):
        return ""
    keys = [
        str(level_index),
        str(level_index + 1),
        ONCALL_LEVEL_LABELS.get(level_index, ""),
        "L%s" % (level_index + 1),
    ]
    for key in keys:
        value = str(display_map.get(key) or "").strip()
        if value:
            return value
    return ""


def format_roster_entries(entries: Iterable[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> str:
    grouped: Dict[int, List[str]] = {}
    for entry in entries or []:
        level_index = safe_int(entry.get("level_index"), 0)
        grouped.setdefault(level_index, []).append(str(entry.get("display_name") or "未知"))
    parts = []
    for level_index in sorted(grouped.keys()):
        configured_display = configured_level_display(cfg, level_index)
        if configured_display:
            parts.append(configured_display)
            continue
        names = "、".join(distinct(grouped[level_index]))
        parts.append("%s：%s" % (ONCALL_LEVEL_LABELS.get(level_index, "L%s" % (level_index + 1)), names))
    return "；".join(parts) or "故障中心通知策略"


def incident_status(resolved: Dict[str, Any]) -> str:
    if not resolved.get("resolved"):
        return "creating"
    return str(((resolved.get("incident") or {}).get("incidents_status") or "open")).lower()


def incident_code(incident: Dict[str, Any], event_created_at: Any) -> str:
    timestamp = incident.get("create_at") or event_created_at
    if safe_int(timestamp, 0) <= 0:
        timestamp = int(time.time())
    if safe_int(timestamp, 0) > 10_000_000_000:
        timestamp = safe_int(timestamp, 0) // 1000
    date_text = time.strftime("%Y%m%d", time.localtime(safe_int(timestamp, int(time.time()))))
    numeric_id = safe_int(incident.get("id"), 0)
    if numeric_id > 0:
        return "INC-%s-%04d" % (date_text, numeric_id)
    incident_uuid = str(incident.get("incident_uuid") or incident.get("uuid") or "")
    if incident_uuid:
        return "INC-%s-%s" % (date_text, incident_uuid[-4:].upper())
    return "INC-%s-NEW" % date_text


def incident_confirm_time(incident: Dict[str, Any], status: str) -> str:
    if status in ("creating", "open"):
        return "待确认"
    status_time = incident.get("status_time") or {}
    if isinstance(status_time, dict):
        for key in ("working", "resolved", "closed", "open"):
            value = status_time.get(key)
            if safe_int(value, 0) > 0:
                return format_time(value)
    return format_time(incident.get("update_at") or incident.get("create_at"))


def card_markdown(cfg: Dict[str, Any], payload: Dict[str, Any], resolved: Dict[str, Any], roster: Dict[str, Any]) -> str:
    event_body = payload.get("event") or {}
    tags = event_body.get("dimension_tags") or {}
    extra = payload.get("extraData") or {}
    incident = resolved.get("incident") or {}
    status = incident_status(resolved)
    code = incident_code(incident, event_body.get("date"))
    feedback = extra.get("feedback_content") or ""
    source = tags.get("lark_chat_name") or "未知群聊"
    current_oncall = roster.get("current_display") or "故障中心通知策略"
    return "\n".join(
        [
            "**Incident：** %s" % code,
            "**级别：** %s" % level_label(incident.get("level")),
            "**来源：** 飞书/Lark：%s" % source,
            "**当前值班人：** %s" % current_oncall,
            "**摘要：** %s" % (feedback or "(empty message)"),
            "**状态：** %s" % status_label(status),
            "**确认时间：** %s" % incident_confirm_time(incident, status),
            "**关联ID：** %s" % (tags.get("lark_oncall_id") or ""),
        ]
    )


def build_lark_card(cfg: Dict[str, Any], payload: Dict[str, Any], resolved: Dict[str, Any]) -> Dict[str, Any]:
    event_body = payload.get("event") or {}
    tags = event_body.get("dimension_tags") or {}
    incident = resolved.get("incident") or {}
    extra = payload.get("extraData") or {}
    roster = resolve_oncall_roster(cfg, resolved)
    status = incident_status(resolved)
    code = incident_code(incident, event_body.get("date"))
    title = "%s %s" % (status_label(status), code)
    value = {
        "component": "guance_oncall_card",
        "action": "refresh",
        "target_status": "open",
        "lark_oncall_id": tags.get("lark_oncall_id"),
        "incident_uuid": incident.get("incident_uuid"),
        "event_ref": (resolved.get("event") or {}).get("event_ref"),
        "chat_id": tags.get("lark_chat_id"),
        "chat_name": tags.get("lark_chat_name"),
        "sender_name": tags.get("lark_sender_name"),
        "feedback_content": extra.get("feedback_content"),
        "lark_message_link": extra.get("lark_message_link"),
        "event_date": event_body.get("date"),
        "allowed_guance_accounts": roster.get("allowed_guance_accounts") or [],
        "allowed_lark_open_ids": roster.get("allowed_lark_open_ids") or [],
        "allowed_lark_user_ids": roster.get("allowed_lark_user_ids") or [],
        "allowed_lark_union_ids": roster.get("allowed_lark_union_ids") or [],
        "allowed_lark_ids": roster.get("allowed_lark_ids") or [],
        "oncall_roster": roster.get("all_members") or [],
    }
    actions = []
    if status == "creating":
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "刷新故障状态"},
                "type": "default",
                "value": value,
            }
        )
    elif status == "open":
        claim_value = dict(value)
        claim_value.update({"action": "claim", "target_status": "working"})
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "待分配"},
                "type": "primary",
                "value": claim_value,
            }
        )
    elif status == "working":
        resolve_value = dict(value)
        resolve_value.update({"action": "resolve", "target_status": "resolved"})
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "处理中"},
                "type": "primary",
                "value": resolve_value,
            }
        )

    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": card_markdown(cfg, payload, resolved, roster)},
        }
    ]
    if actions:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": actions,
                },
            ]
        )
    else:
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "当前故障已解决，卡片不再提供状态变更按钮。"}],
            }
        )

    return {
        "config": {"wide_screen_mode": True, "update_multi": bool((cfg.get("oncall") or {}).get("card_update_multi", True))},
        "header": {
            "template": status_template(status),
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def send_lark_card(cfg: Dict[str, Any], payload: Dict[str, Any], resolved: Dict[str, Any]) -> Dict[str, Any]:
    feishu_cfg = cfg.get("feishu") or {}
    if not feishu_cfg.get("send_card", True):
        log_info("card.skip", reason="disabled")
        return {"sent": False, "reason": "disabled"}
    if not feishu_cfg.get("app_id") or not feishu_cfg.get("app_secret"):
        log_info("card.skip", reason="missing_lark_app_credentials")
        return {"sent": False, "reason": "missing_lark_app_credentials"}

    event_body = payload.get("event") or {}
    tags = event_body.get("dimension_tags") or {}
    chat_id = tags.get("lark_chat_id")
    if not chat_id:
        log_info("card.skip", reason="missing_chat_id")
        return {"sent": False, "reason": "missing_chat_id"}

    token = feishu_tenant_access_token(cfg)
    url = feishu_cfg.get("base_url", "https://open.feishu.cn").rstrip() + "/open-apis/im/v1/messages?receive_id_type=chat_id"
    card = build_lark_card(cfg, payload, resolved)
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    log_info("card.send.start", chat_id=chat_id, lark_oncall_id=tags.get("lark_oncall_id"))
    resp = post_json(
        url,
        body,
        {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": "Bearer " + token,
        },
        timeout=10,
    )
    if resp.get("code") != 0:
        log_info("card.send.failed", code=resp.get("code"), msg=resp.get("msg") or resp.get("message"))
        return {"sent": False, "response": resp}
    data = resp.get("data") or {}
    log_info("card.send.success", message_id=data.get("message_id"), chat_id=data.get("chat_id"))
    remember_card_mapping(cfg, payload, resolved, str(data.get("message_id") or ""), str(data.get("chat_id") or chat_id or ""))
    return {"sent": True, "message_id": data.get("message_id"), "response": resp}


def feishu_auth_headers(token: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "Authorization": "Bearer " + token,
    }


def update_lark_card_message(cfg: Dict[str, Any], message_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
    if not message_id:
        raise OncallFuncError("lark card message_id is required")
    token = feishu_tenant_access_token(cfg)
    feishu_cfg = cfg.get("feishu") or {}
    url = feishu_cfg.get("base_url", "https://open.feishu.cn").rstrip() + "/open-apis/im/v1/messages/%s" % parse.quote(
        str(message_id),
        safe="",
    )
    log_info("card.message.update.start", message_id=message_id)
    resp = request_json(
        url,
        "PATCH",
        feishu_auth_headers(token),
        {"content": json.dumps(card, ensure_ascii=False)},
        timeout=10,
    )
    if resp.get("code") != 0:
        log_info("card.message.update.failed", message_id=message_id, code=resp.get("code"), msg=resp.get("msg") or resp.get("message"))
        raise OncallFuncError("Feishu card update failed: %s" % resp)
    log_info("card.message.update.success", message_id=message_id)
    return resp


def list_lark_messages(cfg: Dict[str, Any], chat_id: str, page_size: int = 50) -> List[Dict[str, Any]]:
    if not chat_id:
        return []
    token = feishu_tenant_access_token(cfg)
    feishu_cfg = cfg.get("feishu") or {}
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "page_size": max(1, min(page_size, 50)),
        "sort_type": "ByCreateTimeDesc",
    }
    url = feishu_cfg.get("base_url", "https://open.feishu.cn").rstrip() + "/open-apis/im/v1/messages?" + parse.urlencode(params)
    try:
        resp = get_json(url, feishu_auth_headers(token), timeout=10)
    except Exception as exc:
        log_info("lark.messages.list.failed", chat_id=chat_id, error=str(exc))
        return []
    if resp.get("code") != 0:
        log_info("lark.messages.list.failed", chat_id=chat_id, code=resp.get("code"), msg=resp.get("msg") or resp.get("message"))
        return []
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    return [item for item in data.get("items") or [] if isinstance(item, dict)]


def find_oncall_card_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        if value.get("component") == "guance_oncall_card":
            return value
        for child in value.values():
            found = find_oncall_card_value(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_oncall_card_value(child)
            if found:
                return found
    return {}


def message_body_content(message: Dict[str, Any]) -> str:
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    content = body.get("content") or message.get("content") or ""
    return str(content or "")


def parse_lark_card_from_message(message: Dict[str, Any]) -> Dict[str, Any]:
    content = message_body_content(message)
    try:
        parsed = json.loads(content) if content else {}
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def find_lark_card_message(
    cfg: Dict[str, Any],
    lark_oncall_id: str = "",
    incident_uuid: str = "",
    event_ref: str = "",
    chat_id: str = "",
) -> Dict[str, Any]:
    identifiers = distinct([lark_oncall_id, incident_uuid, event_ref])
    if not identifiers:
        return {}
    target_chat_ids = distinct([chat_id] + list((cfg.get("lark") or {}).get("target_chat_ids") or []))
    for target_chat_id in target_chat_ids:
        for message in list_lark_messages(cfg, target_chat_id, 50):
            content = message_body_content(message)
            if not all(identifier in content for identifier in identifiers[:1]):
                if not any(identifier and identifier in content for identifier in identifiers):
                    continue
            card = parse_lark_card_from_message(message)
            value = find_oncall_card_value(card)
            if value:
                value_ids = set(
                    distinct(
                        [
                            value.get("lark_oncall_id"),
                            value.get("incident_uuid"),
                            value.get("event_ref"),
                        ]
                    )
                )
                if not value_ids.intersection(set(identifiers)):
                    continue
            message_id = str(message.get("message_id") or message.get("id") or "")
            if message_id:
                log_info(
                    "card.message.found",
                    chat_id=target_chat_id,
                    message_id=message_id,
                    lark_oncall_id=lark_oncall_id,
                    incident_uuid=incident_uuid,
                    event_ref=event_ref,
                )
                return {
                    "message_id": message_id,
                    "chat_id": target_chat_id,
                    "value": value,
                    "card": card,
                }
    return {}


def payload_from_card_value(value: Dict[str, Any]) -> Dict[str, Any]:
    event_date = safe_int(value.get("event_date"), int(time.time()))
    tags = {
        "lark_oncall_id": value.get("lark_oncall_id"),
        "lark_chat_id": value.get("chat_id"),
        "lark_chat_name": value.get("chat_name"),
        "lark_sender_name": value.get("sender_name"),
    }
    return {
        "event": {
            "date": event_date,
            "title": "P0Oncall: %s" % compact_text(value.get("feedback_content") or "", 60),
            "dimension_tags": tags,
        },
        "extraData": {
            "feedback_content": value.get("feedback_content") or "",
            "lark_message_link": value.get("lark_message_link") or "",
        },
    }


def normalize_card_action(raw: Dict[str, Any]) -> Dict[str, Any]:
    event = raw.get("event") if isinstance(raw.get("event"), dict) else raw
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    value = action.get("value")
    if isinstance(value, str):
        value = parse_json_object(value)
    elif not isinstance(value, dict):
        value = {}
    operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
    operator_id = operator.get("operator_id") or event.get("operator_id") or operator.get("user_id") or {}
    operator_open_id = ""
    operator_user_id = ""
    operator_union_id = ""
    if isinstance(operator_id, dict):
        operator_open_id = str(operator_id.get("open_id") or "")
        operator_user_id = str(operator_id.get("user_id") or "")
        operator_union_id = str(operator_id.get("union_id") or "")
    else:
        operator_user_id = str(operator_id or "")
    operator_open_id = operator_open_id or str(operator.get("open_id") or event.get("open_id") or "")
    operator_user_id = operator_user_id or str(operator.get("user_id") or event.get("user_id") or "")
    operator_union_id = operator_union_id or str(operator.get("union_id") or event.get("union_id") or "")
    operator_ids = distinct([operator_open_id, operator_user_id, operator_union_id])
    context = event.get("context") if isinstance(event.get("context"), dict) else {}
    return {
        "operator_open_id": operator_open_id,
        "operator_user_id": operator_user_id,
        "operator_union_id": operator_union_id,
        "operator_ids": operator_ids,
        "value": value,
        "message_id": context.get("open_message_id") or context.get("message_id") or event.get("open_message_id"),
        "chat_id": context.get("open_chat_id") or context.get("chat_id") or value.get("chat_id"),
    }


def card_action_response(message: str, toast_type: str = "info", card: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp: Dict[str, Any] = {
        "toast": {
            "type": toast_type,
            "content": message,
        }
    }
    if card:
        if card.get("type") in ("raw", "template") and isinstance(card.get("data"), dict):
            resp["card"] = card
        else:
            resp["card"] = {"type": "raw", "data": card}
    return resp


def can_operator_act(cfg: Dict[str, Any], value: Dict[str, Any], operator_ids: Iterable[Any]) -> Tuple[bool, str]:
    operator_id_set = set(distinct(operator_ids or []))
    allowed_ids = []
    for key in ("allowed_lark_ids", "allowed_lark_open_ids", "allowed_lark_user_ids", "allowed_lark_union_ids"):
        raw = value.get(key)
        if isinstance(raw, list):
            allowed_ids.extend(raw)
        elif raw:
            allowed_ids.append(raw)
    try:
        current_roster = resolve_oncall_roster(
            cfg,
            {"incident": {"incident_uuid": value.get("incident_uuid") or ""}},
        )
        for key in ("allowed_lark_ids", "allowed_lark_open_ids", "allowed_lark_user_ids", "allowed_lark_union_ids"):
            allowed_ids.extend(current_roster.get(key) or [])
    except Exception as exc:
        log_info("card.permission.roster_refresh_failed", error=str(exc))
    allowed_lark_ids = set(distinct(allowed_ids))
    strict = bool((cfg.get("oncall") or {}).get("strict_card_permission"))
    log_info(
        "card.permission.check",
        operator_ids=list(operator_id_set),
        allowed_lark_ids_count=len(allowed_lark_ids),
        allowed_lark_ids=[short_id(item, 8, 6) for item in sorted(allowed_lark_ids)],
        strict=strict,
    )
    if allowed_lark_ids:
        if operator_id_set.intersection(allowed_lark_ids):
            return True, "ok"
        return False, "operator_not_oncall"
    if strict:
        return False, "missing_lark_mapping"
    return True, "no_mapping_soft_allowed"


def modify_incident_status(cfg: Dict[str, Any], incident_uuid: str, target_status: str, operator_open_id: str) -> Dict[str, Any]:
    if not incident_uuid:
        raise OncallFuncError("incident_uuid is required")
    body: Dict[str, Any] = {"incidentsStatus": target_status}
    operator_account_uuid = guance_account_by_lark_open_id(cfg, operator_open_id)
    if target_status == "working" and operator_account_uuid:
        body["assigner"] = [operator_account_uuid]
    log_info(
        "incident.modify.start",
        incident_uuid=incident_uuid,
        target_status=target_status,
        has_assigner=bool(body.get("assigner")),
    )
    resp = guance_api_post(cfg, "/incidents/%s/modify" % parse.quote(incident_uuid, safe=""), body)
    updated = normalize_incident(unwrap_item(response_content(resp)) or unwrap_item(resp))
    if not updated:
        updated = get_incident_detail(cfg, incident_uuid)
    updated = merge_incident({"incident_uuid": incident_uuid, "incidents_status": target_status}, updated)
    log_info("incident.modify.success", incident_uuid=incident_uuid, incidents_status=updated.get("incidents_status"))
    return updated


def handle_card_action(cfg: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    action = normalize_card_action(raw)
    value = action.get("value") or {}
    if value.get("component") != "guance_oncall_card":
        return card_action_response("这个卡片动作不是 OnCall 流程，已忽略。", "info")

    operator_ids = action.get("operator_ids") or []
    operator_open_id = action.get("operator_open_id") or action.get("operator_user_id") or action.get("operator_union_id") or ""
    allowed, reason = can_operator_act(cfg, value, operator_ids)
    if not allowed:
        if reason == "missing_lark_mapping":
            return card_action_response("还没有配置观测云账号到飞书成员的映射，暂时不能操作。", "warning")
        return card_action_response("只有一线/二线/三线值班人可以操作这个按钮。", "warning")

    requested_action = str(value.get("action") or "refresh")
    target_status = str(value.get("target_status") or "")
    incident_uuid = str(value.get("incident_uuid") or "")
    incident = {}

    try:
        if requested_action in ("claim", "resolve"):
            if target_status not in ("working", "resolved"):
                return card_action_response("卡片状态目标不合法，已拒绝操作。", "warning")
            incident = modify_incident_status(cfg, incident_uuid, target_status, operator_open_id)
        else:
            if incident_uuid:
                incident = get_incident_detail(cfg, incident_uuid)
            elif value.get("event_ref"):
                incident = find_incident(cfg, str(value.get("event_ref")), "P0Oncall", 1) or {}
            if not incident:
                return card_action_response("暂时还没有解析到观测云故障，请稍后再刷新。", "warning")
    except Exception as exc:
        log_info("card.action.failed", action=requested_action, incident_uuid=incident_uuid, error=str(exc))
        return card_action_response("更新观测云故障状态失败：%s" % compact_text(str(exc), 80), "warning")

    payload = payload_from_card_value(value)
    resolved = {
        "resolved": True,
        "event": {"event_ref": value.get("event_ref")},
        "incident": incident,
    }
    card = build_lark_card(cfg, payload, resolved)
    return card_action_response("状态已更新为：%s" % status_label(incident.get("incidents_status")), "success", card)


def recursive_first_by_key(value: Any, keys: Iterable[str]) -> str:
    wanted = {str(key).lower() for key in keys}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in wanted and child not in (None, "", [], {}):
                if isinstance(child, (dict, list)):
                    text = json.dumps(child, ensure_ascii=False, default=str)
                else:
                    text = str(child)
                if text:
                    return text
        for child in value.values():
            found = recursive_first_by_key(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = recursive_first_by_key(child, keys)
            if found:
                return found
    return ""


def hook_text(raw: Dict[str, Any]) -> str:
    return json.dumps(raw, ensure_ascii=False, default=str)


def extract_incident_uuid(raw: Dict[str, Any]) -> str:
    direct = recursive_first_by_key(raw, ["incident_uuid", "incidentUUID", "incidentUuid", "incident_id", "incidentId"])
    if direct.startswith("incident_"):
        return direct
    generic_uuid = recursive_first_by_key(raw, ["uuid"])
    if generic_uuid.startswith("incident_"):
        return generic_uuid
    match = re.search(r"incident_[A-Za-z0-9]+", hook_text(raw))
    return match.group(0) if match else direct


def extract_lark_oncall_id(raw: Dict[str, Any]) -> str:
    direct = recursive_first_by_key(raw, ["lark_oncall_id", "biz_lark_oncall_id", "关联ID"])
    if direct.startswith("ONCALL-"):
        return direct
    match = re.search(r"ONCALL-[A-Z0-9]{8,32}", hook_text(raw))
    return match.group(0) if match else direct


def extract_event_ref(raw: Dict[str, Any]) -> str:
    return recursive_first_by_key(
        raw,
        [
            "event_ref",
            "eventRef",
            "resourceIdentity",
            "resource_identity",
            "df_monitor_checker_event_ref",
        ],
    )


def extract_chat_id(raw: Dict[str, Any]) -> str:
    direct = recursive_first_by_key(raw, ["lark_chat_id", "chat_id", "chatId"])
    return direct if direct.startswith("oc_") else ""


def extract_card_message_id(raw: Dict[str, Any]) -> str:
    return recursive_first_by_key(raw, ["lark_card_message_id", "card_message_id", "cardMessageId"])


def feedback_from_event_message(message: Any, title: Any = "") -> str:
    text = str(message or "")
    for pattern in (r"反馈内容[:：]\s*(.+)", r"摘要[:：]\s*(.+)"):
        match = re.search(pattern, text)
        if match:
            return compact_text(match.group(1), 4000)
    title_text = str(title or "")
    if ":" in title_text:
        return compact_text(title_text.split(":", 1)[1], 4000)
    if "：" in title_text:
        return compact_text(title_text.split("：", 1)[1], 4000)
    return ""


def payload_from_event_item(event_item: Dict[str, Any], lark_oncall_id: str = "") -> Dict[str, Any]:
    tags = dict(event_item.get("dimension_tags") or {})
    if lark_oncall_id and not tags.get("lark_oncall_id"):
        tags["lark_oncall_id"] = lark_oncall_id
    date_value = safe_int(event_item.get("date"), int(time.time()))
    if date_value > 10_000_000_000:
        date_value = date_value // 1000
    feedback = feedback_from_event_message(event_item.get("message"), event_item.get("title"))
    return {
        "event": {
            "date": date_value,
            "title": event_item.get("title") or "P0Oncall",
            "dimension_tags": tags,
        },
        "extraData": {
            "feedback_content": feedback,
            "lark_message_link": "",
        },
    }


def find_incident_by_uuid_or_ref(
    cfg: Dict[str, Any],
    incident_uuid: str = "",
    event_ref: str = "",
    title: str = "P0Oncall",
) -> Dict[str, Any]:
    if incident_uuid:
        detail = get_incident_detail(cfg, incident_uuid)
        if detail:
            return detail
    if event_ref:
        found = find_incident(cfg, event_ref, title, 1)
        if found:
            return found
    return {}


def process_guance_incident_hook(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = config()
    log_info("incident_hook.received", keys=list(raw.keys()), dry_run=cfg.get("dry_run"))

    incident_uuid = extract_incident_uuid(raw)
    lark_oncall_id = extract_lark_oncall_id(raw)
    event_ref = extract_event_ref(raw)
    chat_id = extract_chat_id(raw)
    message_id = extract_card_message_id(raw)

    entry = lookup_card_mapping(cfg, incident_uuid=incident_uuid, lark_oncall_id=lark_oncall_id, event_ref=event_ref)
    if entry:
        incident_uuid = incident_uuid or str(entry.get("incident_uuid") or "")
        lark_oncall_id = lark_oncall_id or str(entry.get("lark_oncall_id") or "")
        event_ref = event_ref or str(entry.get("event_ref") or "")
        chat_id = chat_id or str(entry.get("chat_id") or "")
        message_id = message_id or str(entry.get("message_id") or "")
        log_info(
            "incident_hook.state.hit",
            incident_uuid=incident_uuid,
            lark_oncall_id=lark_oncall_id,
            event_ref=event_ref,
            message_id=message_id,
        )

    incident = find_incident_by_uuid_or_ref(cfg, incident_uuid, event_ref)
    if incident:
        incident_uuid = incident_uuid or str(incident.get("incident_uuid") or "")
        event_ref = event_ref or str(incident.get("resource_identity") or "")

    event_item = {}
    if event_ref:
        incident_create_at = safe_int(incident.get("create_at"), 0)
        incident_create_ms = incident_create_at if incident_create_at > 10_000_000_000 else incident_create_at * 1000
        event_item = query_guance_event_by_ref(cfg, event_ref, incident_create_ms) or {}
        tags = event_item.get("dimension_tags") if isinstance(event_item.get("dimension_tags"), dict) else {}
        lark_oncall_id = lark_oncall_id or str(tags.get("lark_oncall_id") or "")
        chat_id = chat_id or str(tags.get("lark_chat_id") or "")

    if not incident and event_ref:
        incident = find_incident_by_uuid_or_ref(cfg, "", event_ref, event_item.get("title") or "P0Oncall")
        incident_uuid = incident_uuid or str(incident.get("incident_uuid") or "")

    card_lookup = {}
    if not message_id:
        card_lookup = find_lark_card_message(cfg, lark_oncall_id=lark_oncall_id, incident_uuid=incident_uuid, event_ref=event_ref, chat_id=chat_id)
        message_id = str(card_lookup.get("message_id") or "")
        chat_id = chat_id or str(card_lookup.get("chat_id") or "")

    if not incident_uuid or not incident:
        log_info("incident_hook.skip", reason="incident_not_found", incident_uuid=incident_uuid, event_ref=event_ref, lark_oncall_id=lark_oncall_id)
        return {
            "ok": False,
            "updated": False,
            "reason": "incident_not_found",
            "incident_uuid": incident_uuid,
            "event_ref": event_ref,
            "lark_oncall_id": lark_oncall_id,
        }

    if not message_id:
        log_info("incident_hook.skip", reason="card_message_not_found", incident_uuid=incident_uuid, event_ref=event_ref, lark_oncall_id=lark_oncall_id)
        return {
            "ok": False,
            "updated": False,
            "reason": "card_message_not_found",
            "incident_uuid": incident_uuid,
            "event_ref": event_ref,
            "lark_oncall_id": lark_oncall_id,
        }

    card_value = (card_lookup.get("value") if isinstance(card_lookup.get("value"), dict) else {}) or {}
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    if not payload and card_value:
        payload = payload_from_card_value(card_value)
    if not payload and event_item:
        payload = payload_from_event_item(event_item, lark_oncall_id)
    if not payload:
        payload = {
            "event": {
                "date": safe_int(incident.get("create_at"), int(time.time())),
                "title": incident.get("name") or "P0Oncall",
                "dimension_tags": {
                    "lark_oncall_id": lark_oncall_id,
                    "lark_chat_id": chat_id,
                    "lark_chat_name": card_value.get("chat_name") or "",
                },
            },
            "extraData": {
                "feedback_content": card_value.get("feedback_content") or "",
                "lark_message_link": "",
            },
        }

    resolved = {
        "resolved": True,
        "event": {"event_ref": event_ref},
        "incident": incident,
    }
    card = build_lark_card(cfg, payload, resolved)
    update_resp = update_lark_card_message(cfg, message_id, card)
    remember_card_mapping(cfg, payload, resolved, message_id, chat_id)
    log_info(
        "incident_hook.updated",
        incident_uuid=incident_uuid,
        incidents_status=incident.get("incidents_status"),
        lark_oncall_id=lark_oncall_id,
        message_id=message_id,
    )
    return {
        "ok": True,
        "updated": True,
        "incident_uuid": incident_uuid,
        "incidents_status": incident.get("incidents_status"),
        "lark_oncall_id": lark_oncall_id,
        "message_id": message_id,
        "feishu": update_resp,
    }


def nested_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("open_id") or value.get("user_id") or value.get("union_id") or "")
    return str(value or "")


def decode_message_content(message_type: str, content: Any, mentions: Iterable[Dict[str, Any]]) -> str:
    if isinstance(content, str):
        raw = content
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None
    else:
        raw = json.dumps(content, ensure_ascii=False)
        parsed = content

    text = raw
    if isinstance(parsed, dict):
        if message_type == "text":
            text = str(parsed.get("text") or raw)
        elif message_type == "post":
            text = json.dumps(parsed, ensure_ascii=False)

    for item in mentions or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        name = str(item.get("name") or "")
        if key and name:
            text = text.replace(key, "@" + name)
    return text


def normalize_lark_message(raw: Dict[str, Any]) -> Dict[str, Any]:
    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    event = raw.get("event") if isinstance(raw.get("event"), dict) else raw
    message = event.get("message") if isinstance(event.get("message"), dict) else event
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    mentions = message.get("mentions") if isinstance(message.get("mentions"), list) else []
    message_type = str(message.get("message_type") or message.get("msg_type") or "text")

    return {
        "type": header.get("event_type") or raw.get("type") or "im.message.receive_v1",
        "event_id": header.get("event_id") or raw.get("event_id"),
        "chat_id": message.get("chat_id"),
        "chat_type": message.get("chat_type"),
        "message_id": message.get("message_id") or message.get("id"),
        "id": message.get("message_id") or message.get("id"),
        "message_type": message_type,
        "sender_id": nested_id(sender.get("sender_id") or message.get("sender_id")),
        "sender_name": sender.get("sender_name") or message.get("sender_name"),
        "mentions": mentions,
        "content": decode_message_content(message_type, message.get("content"), mentions),
        "message_app_link": message.get("message_app_link") or "",
        "create_time": str(message.get("create_time") or message.get("timestamp") or int(time.time() * 1000)),
        "timestamp": str(message.get("timestamp") or int(time.time() * 1000)),
    }


def verify_lark_token(cfg: Dict[str, Any], raw: Dict[str, Any]) -> None:
    expected = (cfg.get("lark") or {}).get("verify_token") or ""
    if not expected:
        return
    token = str(raw.get("token") or (raw.get("header") or {}).get("token") or "")
    if token != expected:
        raise OncallFuncError("invalid Lark verify token")


def process_lark_callback(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = config()
    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    event_type = header.get("event_type") or raw.get("type")
    log_info("callback.received", event_type=event_type, event_id=header.get("event_id"), dry_run=cfg.get("dry_run"))
    verify_lark_token(cfg, raw)

    if raw.get("type") == "url_verification" and raw.get("challenge"):
        log_info("callback.url_verification")
        return {"challenge": raw.get("challenge")}

    if event_type in ("card.action.trigger", "card.action.trigger_v1"):
        log_info("card.action.received", event_id=header.get("event_id"))
        return handle_card_action(cfg, raw)

    if event_type not in ("im.message.receive_v1", None):
        log_info("callback.skip", reason="event_type_not_supported", event_type=event_type)
        return {"handled": False, "reason": "event_type_not_supported", "event_type": event_type}

    lark_event = normalize_lark_message(raw)
    log_info(
        "lark.normalized",
        chat_id=lark_event.get("chat_id"),
        chat_type=lark_event.get("chat_type"),
        message_id=lark_event.get("message_id"),
        message_type=lark_event.get("message_type"),
        sender_id=lark_event.get("sender_id"),
        content=compact_text(lark_event.get("content"), 120),
    )
    should_handle, reason = should_handle_event(cfg, lark_event)
    if not should_handle:
        log_info("callback.skip", reason=reason, chat_id=lark_event.get("chat_id"), message_id=lark_event.get("message_id"))
        return {"handled": False, "reason": reason, "event": lark_event}

    payload = build_guance_payload(cfg, lark_event)
    lark_oncall_id = payload["event"]["dimension_tags"].get("lark_oncall_id")
    log_info(
        "guance.payload.built",
        title=payload["event"].get("title"),
        lark_oncall_id=lark_oncall_id,
        chat_name=payload["event"]["dimension_tags"].get("lark_chat_name"),
        sender_name=payload["event"]["dimension_tags"].get("lark_sender_name"),
    )

    existing_event = None
    if not cfg.get("dry_run"):
        try:
            existing_event = query_guance_event_once(
                cfg,
                str(lark_oncall_id or ""),
                int(payload["event"].get("date") or time.time()) * 1000,
            )
        except Exception as exc:
            log_info("dedupe.check.failed", lark_oncall_id=lark_oncall_id, error=str(exc))
    if existing_event:
        log_info(
            "dedupe.hit",
            lark_oncall_id=lark_oncall_id,
            doc_id=existing_event.get("doc_id"),
            event_ref=existing_event.get("event_ref"),
        )
        incident = find_incident(cfg, existing_event.get("event_ref") or "", payload["event"].get("title") or "P0Oncall", 1)
        return {
            "handled": True,
            "duplicate": True,
            "lark_oncall_id": lark_oncall_id,
            "guance": {"transport": "skipped_duplicate", "event": existing_event},
            "incident": {"resolved": bool(incident), "event": existing_event, "incident": incident or {}},
            "card": {"sent": False, "reason": "duplicate_callback"},
        }

    send_result = send_http(cfg, payload)
    resolved = resolve_incident(cfg, payload)
    card_result = send_lark_card(cfg, payload, resolved)
    log_info(
        "callback.done",
        lark_oncall_id=payload["event"]["dimension_tags"]["lark_oncall_id"],
        incident_resolved=resolved.get("resolved"),
        card_sent=card_result.get("sent"),
    )
    return {
        "handled": True,
        "lark_oncall_id": payload["event"]["dimension_tags"]["lark_oncall_id"],
        "guance": send_result,
        "incident": resolved,
        "card": card_result,
    }


@DFF.API("feishu-oncall-event")
def feishu_oncall_event(**kwargs):
    try:
        return process_lark_callback(kwargs)
    except Exception as exc:
        log_info("callback.error", error=str(exc))
        return {"handled": False, "error": str(exc)}


@DFF.API("feishu-oncall-incident-hook", timeout=15)
def feishu_oncall_incident_hook(**kwargs):
    try:
        return process_guance_incident_hook(kwargs)
    except Exception as exc:
        log_info("incident_hook.error", error=str(exc))
        return {"ok": False, "updated": False, "error": str(exc)}

