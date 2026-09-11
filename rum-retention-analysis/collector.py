import datetime
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import uuid

# 必填：填写 RUM 应用 ID；该值同时用作 Redis Bitmap 命名空间。
# collector.py 与 metrics.py 必须配置为相同值。
APP_ID = ""
MYSQL_CONNECTOR_ID_DEFAULT = "rum_mysql"
REDIS_CONNECTOR_ID_DEFAULT = "rum_redis"
DATAWAY_CONNECTOR_ID_DEFAULT = "demo_dataway"
BITMAP_RETENTION_DAYS = 90
# 必填：填写观测云 Query Data V1 OpenAPI 完整地址。
# 也可以通过 Func 环境变量 RUM_OPENAPI_URL 覆盖此默认值。
OPENAPI_URL_DEFAULT = ""
TIMEZONE = datetime.timezone(datetime.timedelta(hours=8))
MAX_BACKFILL_DAYS = 365
DEFAULT_PAGE_SIZE = 1000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 必填：根据实际 RUM 数据和业务事件填写三条 DQL。
# DQL_ACTIVE 应返回用户身份及工作空间属性；另外两条分别返回费用中心和普通新增候选用户。
# 查询时间范围由 OpenAPI timeRange 传入，DQL 中不要写固定时间窗口。
DQL_ACTIVE = ""
DQL_COST_CENTER = ""
DQL_ORDINARY = ""

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS rum_user_identity (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        email_normalized VARCHAR(320) NOT NULL,
        user_name VARCHAR(255) NULL,
        userid VARCHAR(128) NULL,
        gc_workspace_id VARCHAR(128) NULL,
        gc_workspace_name VARCHAR(255) NULL,
        bitmap_id BIGINT UNSIGNED NOT NULL,
        first_active_date DATE NULL,
        last_active_date DATE NULL,
        first_registered_date DATE NULL,
        first_cost_center_date DATE NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uk_email_normalized (email_normalized),
        UNIQUE KEY uk_bitmap_id (bitmap_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS rum_bitmap_sequence (
        namespace_key VARCHAR(128) NOT NULL,
        current_seq BIGINT UNSIGNED NOT NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (namespace_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS rum_collect_job (
        job_id VARCHAR(36) NOT NULL,
        job_type VARCHAR(32) NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        status VARCHAR(16) NOT NULL,
        total_days INT UNSIGNED NOT NULL DEFAULT 0,
        completed_days INT UNSIGNED NOT NULL DEFAULT 0,
        result_json JSON NULL,
        error_message VARCHAR(2000) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at DATETIME NULL,
        finished_at DATETIME NULL,
        PRIMARY KEY (job_id),
        KEY idx_status_created (status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS rum_collect_checkpoint (
        window_start DATETIME NOT NULL,
        window_end DATETIME NOT NULL,
        status VARCHAR(16) NOT NULL,
        active_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        ordinary_candidate_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        cost_center_candidate_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        invalid_email_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        openapi_warning_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        execution_duration_ms BIGINT UNSIGNED NOT NULL DEFAULT 0,
        error_message VARCHAR(2000) NULL,
        started_at DATETIME NULL,
        finished_at DATETIME NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (window_start, window_end),
        KEY idx_status_window (status, window_end)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS rum_collect_day (
        stat_date DATE NOT NULL,
        status VARCHAR(16) NOT NULL,
        active_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        ordinary_candidate_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        cost_center_candidate_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        ordinary_first_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        cost_center_first_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        invalid_email_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
        warning_json JSON NULL,
        error_message VARCHAR(2000) NULL,
        started_at DATETIME NULL,
        finished_at DATETIME NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (stat_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
]


def _mysql():
    return DFF.CONN(DFF.ENV("RUM_MYSQL_CONNECTOR_ID", default=MYSQL_CONNECTOR_ID_DEFAULT))


def _redis():
    return DFF.CONN(DFF.ENV("RUM_REDIS_CONNECTOR_ID", default=REDIS_CONNECTOR_ID_DEFAULT))


def _dataway():
    return DFF.CONN(DFF.ENV("RUM_DATAWAY_CONNECTOR_ID", default=DATAWAY_CONNECTOR_ID_DEFAULT))


def _env_required(name):
    value = DFF.ENV(name)
    if not value:
        raise RuntimeError("Missing required Func environment variable: %s" % name)
    return value


def _log(event, **fields):
    record = {"event": event, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))


def _parse_date(value):
    if not value:
        raise ValueError("date is required")
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("date must use YYYY-MM-DD")


def _date_bounds_ms(day):
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=TIMEZONE)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


def _normalize_email(value):
    if value is None:
        return None
    email = str(value).strip().lower()
    if not email or len(email) > 320 or not EMAIL_RE.match(email):
        return None
    return email


def _invalid_email_detail(value):
    if value is None:
        reason = "null"
        raw = ""
    else:
        raw = str(value)
        stripped = raw.strip()
        if not stripped:
            reason = "empty"
        elif len(stripped) > 320:
            reason = "too_long"
        elif any(char.isspace() for char in stripped):
            reason = "contains_whitespace"
        elif "@" not in stripped:
            reason = "missing_at"
        elif not EMAIL_RE.match(stripped.lower()):
            reason = "bad_format"
        else:
            reason = "unknown"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    if not raw:
        masked = "<empty>"
    elif "@" in raw:
        local, domain = raw.rsplit("@", 1)
        masked = (local[:2] + "***" if local else "***") + "@" + domain[:80]
    else:
        masked = raw[:2] + "***" if len(raw) > 2 else "***"
    return {"reason": reason, "raw": raw, "masked": masked, "hash": digest, "length": len(raw)}


def _log_invalid_emails(day_or_window, source, invalid_details):
    counts = {}
    for detail in invalid_details:
        key = (detail["reason"], detail["raw"], detail["masked"], detail["hash"], detail["length"])
        counts[key] = counts.get(key, 0) + 1
    for (reason, raw_value, masked, digest, length), count in counts.items():
        _log(
            "invalid_email",
            period=str(day_or_window),
            source=source,
            reason=reason,
            raw_value=raw_value,
            masked_value=masked,
            value_hash=digest,
            value_length=length,
            count=count,
        )


def _post_openapi(payload):
    url = DFF.ENV("RUM_OPENAPI_URL", default=OPENAPI_URL_DEFAULT)
    api_key = _env_required("RUM_OPENAPI_API_KEY")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "DF-API-KEY": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Query Data HTTP %s: %s" % (exc.code, detail[:500]))
    if result.get("code") != 200:
        raise RuntimeError("Query Data business error: %s" % json.dumps(result, ensure_ascii=False)[:800])
    return result


def _iter_rows(query_result):
    columns = query_result.get("columns") or query_result.get("column_names") or []
    for series in query_result.get("series") or []:
        series_columns = series.get("columns") or columns
        for value in series.get("values") or []:
            row = value if isinstance(value, list) else [value]
            if series_columns and len(series_columns) == len(row):
                yield dict(zip(series_columns, row))
            else:
                yield row


def _pick_value(row, field):
    aliases = [field, "`%s`" % field, "distinct(%s)" % field, "distinct(`%s`)" % field]
    if isinstance(row, dict):
        for key in aliases:
            if key in row:
                return row[key]
        for key, value in row.items():
            normalized_key = str(key).replace("`", "")
            if normalized_key == field or normalized_key == "distinct(%s)" % field:
                return value
        return None
    if isinstance(row, list):
        non_time = [v for v in row if not isinstance(v, (int, float))]
        return non_time[-1] if non_time else (row[-1] if row else None)
    return row


def _query_emails_range(dql, email_field, start_ms, end_ms, page_size=None):
    page_size = int(page_size or DFF.ENV("RUM_QUERY_PAGE_SIZE", default=DEFAULT_PAGE_SIZE))
    offset = 0
    search_after = None
    emails = set()
    warnings = []
    invalid_count = 0
    invalid_details = []
    seen_pages = 0
    while True:
        query = {
            "q": dql,
            "timeRange": [start_ms, end_ms],
            "tz": "Asia/Shanghai",
            "limit": page_size,
            "disable_sampling": True,
            "ignore_cache": True,
        }
        if search_after is not None:
            query["search_after"] = search_after
        elif offset:
            query["offset"] = offset
        result = _post_openapi({"queries": [{"qtype": "dql", "query": query}]})
        data = (result.get("content") or {}).get("data") or []
        if not data:
            break
        query_result = data[0]
        page_warnings = query_result.get("warnings") or []
        if page_warnings:
            warnings.extend(page_warnings)
        rows = []
        for series in query_result.get("series") or []:
            columns = series.get("columns") or query_result.get("columns") or query_result.get("column_names") or []
            normalized_columns = [str(column).replace("`", "") for column in columns]
            accepted = email_field in normalized_columns or ("distinct(%s)" % email_field) in normalized_columns
            if not accepted:
                continue
            for value in series.get("values") or []:
                row_values = value if isinstance(value, list) else [value]
                row = dict(zip(columns, row_values)) if columns and len(columns) == len(row_values) else row_values
                rows.append(row)
                picked = _pick_value(row, email_field)
                email = _normalize_email(picked)
                if email:
                    emails.add(email)
                else:
                    invalid_count += 1
                    invalid_details.append(_invalid_email_detail(picked))
        seen_pages += 1
        next_marker = query_result.get("search_after")
        if next_marker and next_marker != search_after:
            search_after = next_marker
        elif len(rows) >= page_size:
            offset += page_size
        else:
            break
        if not rows or seen_pages > 100000:
            break
    return emails, invalid_count, warnings, invalid_details


def _query_active_profiles_range(start_ms, end_ms, page_size=None):
    page_size = int(page_size or DFF.ENV("RUM_QUERY_PAGE_SIZE", default=DEFAULT_PAGE_SIZE))
    offset = 0
    profiles = {}
    warnings = []
    invalid_count = 0
    invalid_details = []
    while True:
        query = {
            "q": DQL_ACTIVE, "timeRange": [start_ms, end_ms], "tz": "Asia/Shanghai",
            "limit": page_size, "disable_sampling": True, "ignore_cache": True,
        }
        if offset:
            query["offset"] = offset
        result = _post_openapi({"queries": [{"qtype": "dql", "query": query}]})
        data = (result.get("content") or {}).get("data") or []
        if not data:
            break
        qr = data[0]
        warnings.extend(qr.get("warnings") or [])
        fields_by_time = {}
        email_rows = 0
        for series in qr.get("series") or []:
            columns = series.get("columns") or []
            names = [str(column).replace("`", "") for column in columns]
            target = next((name for name in ("user_email", "user_name", "userid", "gc_workspace_id", "gc_workspace_name") if name in names), None)
            if not target or "time" not in names:
                continue
            ti, vi = names.index("time"), names.index(target)
            for row in series.get("values") or []:
                items = row if isinstance(row, list) else [row]
                if ti >= len(items) or vi >= len(items):
                    continue
                fields_by_time.setdefault(items[ti], {})[target] = items[vi]
                if target == "user_email":
                    email_rows += 1
        for values in fields_by_time.values():
            email = _normalize_email(values.get("user_email"))
            if not email:
                if "user_email" in values:
                    invalid_count += 1
                    invalid_details.append(_invalid_email_detail(values.get("user_email")))
                continue
            current = profiles.setdefault(email, {"user_name": None, "userid": None, "gc_workspace_id": None, "gc_workspace_name": None})
            if values.get("user_name") not in (None, "", "undefined"):
                current["user_name"] = str(values["user_name"])[:255]
            if values.get("userid") not in (None, "", "undefined"):
                current["userid"] = str(values["userid"])[:128]
            if values.get("gc_workspace_id") not in (None, "", "undefined"):
                current["gc_workspace_id"] = str(values["gc_workspace_id"])[:128]
            if values.get("gc_workspace_name") not in (None, "", "undefined"):
                current["gc_workspace_name"] = str(values["gc_workspace_name"])[:255]
        if email_rows >= page_size:
            offset += page_size
        else:
            break
    return profiles, invalid_count, warnings, invalid_details


def _update_identity_profiles(profiles):
    db = _mysql()
    for email, profile in profiles.items():
        if all(profile.get(key) is None for key in ("user_name", "userid", "gc_workspace_id", "gc_workspace_name")):
            continue
        db.non_query(
            "UPDATE rum_user_identity SET user_name=COALESCE(?, user_name), userid=COALESCE(?, userid), gc_workspace_id=COALESCE(?, gc_workspace_id), gc_workspace_name=COALESCE(?, gc_workspace_name) WHERE email_normalized=?",
            sql_params=[profile.get("user_name"), profile.get("userid"), profile.get("gc_workspace_id"), profile.get("gc_workspace_name"), email],
        )


def _chunks(items, size=500):
    values = list(items)
    for pos in range(0, len(values), size):
        yield values[pos:pos + size]


def _load_identity_rows(emails):
    db = _mysql()
    result = {}
    for batch in _chunks(sorted(emails)):
        rows = db.query(
            "SELECT id, email_normalized, user_name, userid, gc_workspace_id, gc_workspace_name, bitmap_id, first_active_date, last_active_date, first_registered_date, first_cost_center_date FROM rum_user_identity WHERE email_normalized IN (?)",
            sql_params=[batch],
        )
        for row in rows:
            result[row["email_normalized"]] = row
    return result


def _ensure_identities(emails):
    emails = sorted(set(emails))
    if not emails:
        return {}
    existing = _load_identity_rows(emails)
    missing = [email for email in emails if email not in existing]
    if missing:
        db = _mysql()
        trans = db.start_trans()
        try:
            db.trans_non_query(
                trans,
                "INSERT INTO rum_bitmap_sequence (namespace_key, current_seq) VALUES (?, 0) ON DUPLICATE KEY UPDATE namespace_key = VALUES(namespace_key)",
                [APP_ID],
            )
            seq_rows = db.trans_query(
                trans,
                "SELECT current_seq FROM rum_bitmap_sequence WHERE namespace_key = ? FOR UPDATE",
                [APP_ID],
            )
            current = int(seq_rows[0]["current_seq"])
            end_seq = current + len(missing)
            db.trans_non_query(
                trans,
                "UPDATE rum_bitmap_sequence SET current_seq = ? WHERE namespace_key = ?",
                [end_seq, APP_ID],
            )
            values = [[email, current + index + 1] for index, email in enumerate(missing)]
            db.trans_non_query(
                trans,
                "INSERT IGNORE INTO rum_user_identity (email_normalized, bitmap_id) VALUES ?",
                [values],
            )
            db.commit(trans)
        except Exception:
            db.rollback(trans)
            raise
        existing = _load_identity_rows(emails)
        unresolved = [email for email in emails if email not in existing]
        if unresolved:
            raise RuntimeError("Failed to create %s identity mappings" % len(unresolved))
    return existing


def _replace_bitmap(key, bitmap_ids):
    ids = sorted(set(int(value) for value in bitmap_ids))
    redis = _redis()
    temp_key = key + ":tmp:" + uuid.uuid4().hex
    if not ids:
        redis.query("DEL", key)
        return 0
    lua = "for i=1,#ARGV do redis.call('SETBIT',KEYS[1],ARGV[i],1) end return #ARGV"
    total = 0
    try:
        for batch in _chunks(ids, 1000):
            result = redis.query("EVAL", lua, 1, temp_key, *batch)
            total += int(result or 0)
        redis.query("RENAME", temp_key, key)
        return total
    except Exception:
        redis.query("DEL", temp_key)
        raise


def _daily_expire_at(day):
    expire_at = datetime.datetime.combine(
        day + datetime.timedelta(days=BITMAP_RETENTION_DAYS + 1),
        datetime.time.min,
        tzinfo=TIMEZONE,
    )
    return int(expire_at.timestamp())


def _expire_daily_bitmap(key, day):
    if int(_redis().query("EXISTS", key) or 0):
        _redis().query("EXPIREAT", key, _daily_expire_at(day))


def _complete_history_days(day, max_days=30):
    start = day - datetime.timedelta(days=max_days)
    daily_rows = _mysql().query(
        "SELECT stat_date FROM rum_collect_day WHERE stat_date>=? AND stat_date<? AND status='succeeded'",
        sql_params=[start, day],
    )
    complete = {_parse_date(row["stat_date"]) for row in daily_rows}
    checkpoint_rows = _mysql().query(
        """SELECT DATE(window_start) AS stat_date, COUNT(*) AS succeeded
        FROM rum_collect_checkpoint
        WHERE window_start>=? AND window_start<? AND status='succeeded'
        GROUP BY DATE(window_start)""",
        sql_params=[
            datetime.datetime.combine(start, datetime.time.min),
            datetime.datetime.combine(day, datetime.time.min),
        ],
    )
    for row in checkpoint_rows:
        if int(row["succeeded"]) == 288:
            complete.add(_parse_date(row["stat_date"]))
    return complete


def _classify_return_users(day, rows, active_emails):
    if not active_emails:
        return []
    complete_days = _complete_history_days(day, 30)
    allow = {
        days: all(day - datetime.timedelta(days=i) in complete_days for i in range(1, days + 1))
        for days in (3, 7, 30)
    }
    if not allow[3]:
        return []
    redis = _redis()
    recent_keys = []
    detected = []
    return_keys = {
        3: _bitmap_key("return_3d", day),
        7: _bitmap_key("return_7d", day),
        30: _bitmap_key("return_30d", day),
    }
    try:
        for days in (3, 7, 30):
            keys = [_bitmap_key("active", day - datetime.timedelta(days=i)) for i in range(1, days + 1)]
            recent_key = "rum:{%s}:tmp:return:recent_%sd:%s" % (APP_ID, days, uuid.uuid4().hex)
            redis.query("BITOP", "OR", recent_key, *keys)
            redis.query("EXPIRE", recent_key, 3600)
            recent_keys.append(recent_key)
        lua = """local out = {}
        local allow7 = ARGV[1] == '1'
        local allow30 = ARGV[2] == '1'
        for i=3,#ARGV do
            local id = ARGV[i]
            if redis.call('GETBIT', KEYS[4], id) == 0 and redis.call('GETBIT', KEYS[1], id) == 0 then
                local level = 3
                if allow7 and redis.call('GETBIT', KEYS[2], id) == 0 then level = 7 end
                if allow30 and level == 7 and redis.call('GETBIT', KEYS[3], id) == 0 then level = 30 end
                local first = redis.call('GETBIT', KEYS[5], id) == 0
                redis.call('SETBIT', KEYS[5], id, 1)
                if level >= 7 then redis.call('SETBIT', KEYS[6], id, 1) end
                if level >= 30 then redis.call('SETBIT', KEYS[7], id, 1) end
                if first then
                    table.insert(out, id)
                    table.insert(out, level)
                end
            end
        end
        return out"""
        emails_by_id = {int(rows[email]["bitmap_id"]): email for email in active_emails}
        ids = sorted(emails_by_id)
        for batch in _chunks(ids, 500):
            result = redis.query(
                "EVAL", lua, 7,
                recent_keys[0], recent_keys[1], recent_keys[2],
                _bitmap_key("ordinary_new", day),
                return_keys[3], return_keys[7], return_keys[30],
                "1" if allow[7] else "0",
                "1" if allow[30] else "0",
                *batch
            ) or []
            for pos in range(0, len(result), 2):
                bitmap_id = int(result[pos])
                detected.append((emails_by_id[bitmap_id], bitmap_id, int(result[pos + 1])))
        for key in return_keys.values():
            _expire_daily_bitmap(key, day)
        return detected
    finally:
        if recent_keys:
            redis.query("DEL", *recent_keys)


def _write_return_logs(day, detected, profiles):
    if not detected:
        return {"point_count": 0}
    now = datetime.datetime.now(TIMEZONE)
    points = []
    for email, bitmap_id, level in detected:
        profile = profiles.get(email) or {}
        fields = {
            "message": "RUM用户回流",
            "email": email,
            "bitmap_id": bitmap_id,
            "return_level": level,
            "inactive_days_at_least": level,
            "stat_date": day.isoformat(),
            "detected_at": now.isoformat(),
        }
        for key in ("userid", "user_name", "gc_workspace_id", "gc_workspace_name"):
            value = profile.get(key)
            if value not in (None, "", "undefined"):
                fields[key] = str(value)
        points.append({
            "measurement": "rum_user_return",
            "tags": {
                "signal_type": "user_return",
                "return_level": str(level),
                "app_id": APP_ID,
            },
            "fields": fields,
            "timestamp": int(now.timestamp()),
        })
    status_code, result = _dataway().write_by_category_many(category="logging", data=points)
    if not 200 <= int(status_code) < 300:
        raise RuntimeError("return log DataWay HTTP %s: %s" % (status_code, str(result)[:1000]))
    return {"point_count": len(points), "status_code": int(status_code)}


def _move_earlier_first_bits(day, rows, ordinary_candidates, cost_candidates):
    redis = _redis()
    for email in ordinary_candidates:
        previous = rows[email].get("first_registered_date")
        if previous and str(previous) > day.isoformat():
            redis.query("SETBIT", _bitmap_key("ordinary_new", _parse_date(previous)), int(rows[email]["bitmap_id"]), 0)
    for email in cost_candidates:
        previous = rows[email].get("first_cost_center_date")
        if previous and str(previous) > day.isoformat():
            redis.query("SETBIT", _bitmap_key("cost_center_new", _parse_date(previous)), int(rows[email]["bitmap_id"]), 0)


def _bitmap_key(kind, day):
    return "rum:{%s}:%s:%s" % (APP_ID, kind, day.isoformat())


def _update_identity_dates(day, active, ordinary, cost_center):
    db = _mysql()
    for batch in _chunks(sorted(active)):
        db.non_query(
            "UPDATE rum_user_identity SET first_active_date = COALESCE(LEAST(first_active_date, ?), ?), last_active_date = COALESCE(GREATEST(last_active_date, ?), ?) WHERE email_normalized IN (?)",
            sql_params=[day, day, day, day, batch],
        )
    for batch in _chunks(sorted(ordinary)):
        db.non_query(
            "UPDATE rum_user_identity SET first_registered_date = COALESCE(LEAST(first_registered_date, ?), ?) WHERE email_normalized IN (?)",
            sql_params=[day, day, batch],
        )
    for batch in _chunks(sorted(cost_center)):
        db.non_query(
            "UPDATE rum_user_identity SET first_cost_center_date = COALESCE(LEAST(first_cost_center_date, ?), ?) WHERE email_normalized IN (?)",
            sql_params=[day, day, batch],
        )


def _first_sets(day, rows, ordinary_candidates, cost_candidates):
    ordinary_first = set()
    cost_first = set()
    for email in ordinary_candidates:
        value = rows[email].get("first_registered_date")
        if value is None or str(value) >= day.isoformat():
            ordinary_first.add(email)
    for email in cost_candidates:
        value = rows[email].get("first_cost_center_date")
        if value is None or str(value) >= day.isoformat():
            cost_first.add(email)
    return ordinary_first, cost_first


def _acquire_lock(day):
    key = "rum:{%s}:lock:collect:%s" % (APP_ID, day.isoformat())
    token = uuid.uuid4().hex
    result = _redis().query("SET", key, token, "NX", "EX", 3600)
    return key, token if result else (key, None)


def _release_lock(key, token):
    if not token:
        return
    lua = "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end"
    _redis().query("EVAL", lua, 1, key, token)


def _collect_one_day(day, force=False):
    db = _mysql()
    force = bool(force)
    previous_rows = db.query(
        "SELECT status, active_count FROM rum_collect_day WHERE stat_date=?",
        sql_params=[day],
    )
    previous_active = int(previous_rows[0]["active_count"] or 0) if previous_rows else 0
    lock_key = "rum:{%s}:lock:collect:%s" % (APP_ID, day.isoformat())
    token = uuid.uuid4().hex
    locked = _redis().query("SET", lock_key, token, "NX", "EX", 3600)
    if not locked:
        raise RuntimeError("Collection for %s is already running" % day)
    try:
        db.non_query(
            "INSERT INTO rum_collect_day (stat_date, status, started_at) VALUES (?, 'running', NOW()) ON DUPLICATE KEY UPDATE status='running', started_at=NOW(), error_message=NULL",
            sql_params=[day],
        )
        active_profiles, invalid_a, warnings_a, details_a = _query_active_profiles_range(*_date_bounds_ms(day))
        active = set(active_profiles)
        cost, invalid_c, warnings_c, details_c = _query_emails_range(DQL_COST_CENTER, "register_email", *_date_bounds_ms(day))
        ordinary_action, invalid_o, warnings_o, details_o = _query_emails_range(DQL_ORDINARY, "user_email", *_date_bounds_ms(day))
        _log_invalid_emails(day, "active", details_a)
        _log_invalid_emails(day, "cost_center", details_c)
        _log_invalid_emails(day, "ordinary", details_o)
        ordinary_candidates = ordinary_action | cost
        # Historical OpenAPI retention can expire or become partial. Guard before
        # any identity/date/Bitmap mutation so a bad rebuild cannot destroy good data.
        today = datetime.datetime.now(TIMEZONE).date()
        if day < today and previous_active > 0 and not force:
            current_active = len(active)
            minimum_expected = max(1, int(previous_active * 0.8))
            if current_active == 0 or current_active < minimum_expected:
                raise RuntimeError(
                    "historical rebuild safeguard: %s active dropped from %s to %s; "
                    "refusing to replace Bitmap (pass force=true only after validating source completeness)"
                    % (day.isoformat(), previous_active, current_active)
                )
        all_emails = active | ordinary_candidates | cost
        rows_before = _ensure_identities(all_emails)
        _update_identity_profiles(active_profiles)
        ordinary_first, cost_first = _first_sets(day, rows_before, ordinary_candidates, cost)
        _move_earlier_first_bits(day, rows_before, ordinary_candidates, cost)
        _update_identity_dates(day, active, ordinary_candidates, cost)
        rows = _load_identity_rows(all_emails)
        _replace_bitmap(_bitmap_key("active", day), [rows[e]["bitmap_id"] for e in active])
        _replace_bitmap(_bitmap_key("ordinary_new", day), [rows[e]["bitmap_id"] for e in ordinary_first])
        _replace_bitmap(_bitmap_key("cost_center_new", day), [rows[e]["bitmap_id"] for e in cost_first])
        for kind in ("active", "ordinary_new", "cost_center_new"):
            _expire_daily_bitmap(_bitmap_key(kind, day), day)
        _redis().query(
            "DEL",
            _bitmap_key("return_3d", day),
            _bitmap_key("return_7d", day),
            _bitmap_key("return_30d", day),
        )
        _classify_return_users(day, rows, active)
        warning_data = warnings_a + warnings_c + warnings_o
        output = {
            "date": day.isoformat(),
            "active": len(active),
            "ordinary_candidates": len(ordinary_candidates),
            "cost_center_candidates": len(cost),
            "ordinary_first": len(ordinary_first),
            "cost_center_first": len(cost_first),
            "invalid_emails": invalid_a + invalid_c + invalid_o,
            "warnings": len(warning_data),
        }
        db.non_query(
            "UPDATE rum_collect_day SET status='succeeded', active_count=?, ordinary_candidate_count=?, cost_center_candidate_count=?, ordinary_first_count=?, cost_center_first_count=?, invalid_email_count=?, warning_json=?, finished_at=NOW(), error_message=NULL WHERE stat_date=?",
            sql_params=[
                len(active), len(ordinary_candidates), len(cost), len(ordinary_first),
                len(cost_first), invalid_a + invalid_c + invalid_o,
                json.dumps(warning_data, ensure_ascii=False, default=str), day,
            ],
        )
        _log("collect_day_succeeded", **output)
        return output
    except Exception as exc:
        db.non_query(
            "INSERT INTO rum_collect_day (stat_date, status, error_message, finished_at) VALUES (?, 'failed', ?, NOW()) ON DUPLICATE KEY UPDATE status='failed', error_message=VALUES(error_message), finished_at=NOW()",
            sql_params=[day, str(exc)[:2000]],
        )
        _log("collect_day_failed", date=day.isoformat(), error=str(exc)[:500])
        raise
    finally:
        _release_lock(lock_key, token)


def _append_bitmap_bits(key, bitmap_ids):
    ids = sorted(set(int(value) for value in bitmap_ids))
    if not ids:
        return 0
    redis = _redis()
    lua = "for i=1,#ARGV do redis.call('SETBIT',KEYS[1],ARGV[i],1) end return #ARGV"
    total = 0
    for batch in _chunks(ids, 1000):
        total += int(redis.query("EVAL", lua, 1, key, *batch) or 0)
    return total


def _collect_window(start_dt, end_dt):
    task_started = time.time()
    if end_dt <= start_dt:
        raise ValueError("window end must be after start")
    if start_dt.date() != (end_dt - datetime.timedelta(milliseconds=1)).date():
        raise ValueError("incremental window must stay within one natural day")
    day = start_dt.date()
    db = _mysql()
    start_naive = start_dt.replace(tzinfo=None)
    end_naive = end_dt.replace(tzinfo=None)
    existing = db.query(
        "SELECT status FROM rum_collect_checkpoint WHERE window_start=? AND window_end=?",
        sql_params=[start_naive, end_naive],
    )
    if existing and existing[0]["status"] == "succeeded":
        return {"ok": True, "status": "succeeded", "idempotent": True, "window_start": start_dt.isoformat(), "window_end": end_dt.isoformat()}
    lock_key = "rum:{%s}:lock:window:%s" % (APP_ID, start_dt.strftime("%Y%m%d%H%M"))
    token = uuid.uuid4().hex
    if not _redis().query("SET", lock_key, token, "NX", "EX", 600):
        raise RuntimeError("Incremental window is already running")
    try:
        db.non_query(
            "INSERT INTO rum_collect_checkpoint (window_start, window_end, status, started_at) VALUES (?, ?, 'running', NOW()) ON DUPLICATE KEY UPDATE status='running', started_at=NOW(), error_message=NULL",
            sql_params=[start_naive, end_naive],
        )
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000) - 1
        active_profiles, invalid_a, warnings_a, details_a = _query_active_profiles_range(start_ms, end_ms)
        active = set(active_profiles)
        cost, invalid_c, warnings_c, details_c = _query_emails_range(DQL_COST_CENTER, "register_email", start_ms, end_ms)
        ordinary_action, invalid_o, warnings_o, details_o = _query_emails_range(DQL_ORDINARY, "user_email", start_ms, end_ms)
        period = "%s/%s" % (start_dt.isoformat(), end_dt.isoformat())
        _log_invalid_emails(period, "active", details_a)
        _log_invalid_emails(period, "cost_center", details_c)
        _log_invalid_emails(period, "ordinary", details_o)
        ordinary_candidates = ordinary_action | cost
        all_emails = active | ordinary_candidates
        rows_before = _ensure_identities(all_emails)
        _update_identity_profiles(active_profiles)
        ordinary_first, cost_first = _first_sets(day, rows_before, ordinary_candidates, cost)
        _move_earlier_first_bits(day, rows_before, ordinary_candidates, cost)
        _update_identity_dates(day, active, ordinary_candidates, cost)
        rows = _load_identity_rows(all_emails)
        _append_bitmap_bits(_bitmap_key("active", day), [rows[e]["bitmap_id"] for e in active])
        _append_bitmap_bits(_bitmap_key("ordinary_new", day), [rows[e]["bitmap_id"] for e in ordinary_first])
        _append_bitmap_bits(_bitmap_key("cost_center_new", day), [rows[e]["bitmap_id"] for e in cost_first])
        for kind in ("active", "ordinary_new", "cost_center_new"):
            _expire_daily_bitmap(_bitmap_key(kind, day), day)
        detected_returns = _classify_return_users(day, rows, active)
        try:
            return_log_result = _write_return_logs(day, detected_returns, active_profiles)
        except Exception:
            for email, bitmap_id, level in detected_returns:
                _redis().query("SETBIT", _bitmap_key("return_3d", day), bitmap_id, 0)
                if level >= 7:
                    _redis().query("SETBIT", _bitmap_key("return_7d", day), bitmap_id, 0)
                if level >= 30:
                    _redis().query("SETBIT", _bitmap_key("return_30d", day), bitmap_id, 0)
            raise
        warnings = warnings_a + warnings_c + warnings_o
        if warnings:
            _log("collect_window_warnings", window_start=start_dt.isoformat(), warning_count=len(warnings))
        db.non_query(
            "UPDATE rum_collect_checkpoint SET status='succeeded', active_count=?, ordinary_candidate_count=?, cost_center_candidate_count=?, invalid_email_count=?, openapi_warning_count=?, execution_duration_ms=?, finished_at=NOW(), error_message=NULL WHERE window_start=? AND window_end=?",
            sql_params=[len(active), len(ordinary_candidates), len(cost), invalid_a + invalid_c + invalid_o, len(warnings), int((time.time()-task_started)*1000), start_naive, end_naive],
        )
        DFF.FUNC("__metrics.write_window_quality", kwargs={"window_start": start_dt.isoformat(), "window_end": end_dt.isoformat()}, timeout=120, expires=600)
        return {"ok": True, "status": "succeeded", "window_start": start_dt.isoformat(), "window_end": end_dt.isoformat(), "active": len(active), "ordinary_candidates": len(ordinary_candidates), "cost_center_candidates": len(cost), "return_signals": len(detected_returns), "return_logs": return_log_result, "invalid_emails": invalid_a + invalid_c + invalid_o, "warnings": len(warnings)}
    except Exception as exc:
        db.non_query(
            "INSERT INTO rum_collect_checkpoint (window_start, window_end, status, error_message, execution_duration_ms, finished_at) VALUES (?, ?, 'failed', ?, ?, NOW()) ON DUPLICATE KEY UPDATE status='failed', error_message=VALUES(error_message), execution_duration_ms=VALUES(execution_duration_ms), finished_at=NOW()",
            sql_params=[start_naive, end_naive, str(exc)[:2000], int((time.time()-task_started)*1000)],
        )
        raise
    finally:
        _release_lock(lock_key, token)


@DFF.API("每五分钟增量采集 RUM 用户", category="rum-retention", tags=["cron"], timeout=300)
def collect_incremental():
    now = datetime.datetime.now(TIMEZONE)
    delayed = now - datetime.timedelta(minutes=10)
    end = delayed.replace(minute=(delayed.minute // 5) * 5, second=0, microsecond=0)
    start = end - datetime.timedelta(minutes=5)
    return _collect_window(start, end)


@DFF.API("重置 RUM 留存派生数据", category="rum-retention", tags=["setup"], timeout=300)
def reset_derived_data(confirm):
    if confirm != "RESET_RUM_RETENTION":
        raise ValueError("confirmation text mismatch")
    redis = _redis()
    cursor = 0
    deleted_keys = 0
    pattern = "rum:{%s}:*" % APP_ID
    while True:
        result = redis.query("SCAN", cursor, "MATCH", pattern, "COUNT", 500)
        cursor = int(result[0])
        keys = result[1] or []
        if keys:
            deleted_keys += int(redis.query("DEL", *keys) or 0)
        if cursor == 0:
            break
    db = _mysql()
    trans = db.start_trans()
    try:
        for table in ("rum_collect_checkpoint", "rum_collect_day", "rum_collect_job", "rum_user_identity"):
            db.trans_non_query(trans, "DELETE FROM ??", [table])
        db.trans_non_query(trans, "UPDATE rum_bitmap_sequence SET current_seq=0 WHERE namespace_key=?", [APP_ID])
        db.commit(trans)
    except Exception:
        db.rollback(trans)
        raise
    return {"ok": True, "redis_keys_deleted": deleted_keys, "mysql_data_reset": True, "sequence": 0}


@DFF.API("标记 RUM 历史日不完整", category="rum-retention", tags=["maintenance"], timeout=60)
def mark_collect_day_incomplete(stat_date, reason):
    day = _parse_date(stat_date)
    _mysql().non_query(
        "UPDATE rum_collect_day SET status='failed', error_message=? WHERE stat_date=?",
        sql_params=[str(reason)[:2000], day],
    )
    return {"ok": True, "stat_date": day.isoformat(), "status": "failed"}


@DFF.API("清理 RUM 回流等级", category="rum-retention", tags=["maintenance"], timeout=60)
def clear_return_level(stat_date, return_days):
    day = _parse_date(stat_date)
    return_days = int(return_days)
    if return_days not in (3, 7, 30):
        raise ValueError("return_days must be 3, 7, or 30")
    key = _bitmap_key("return_%sd" % return_days, day)
    deleted = int(_redis().query("DEL", key) or 0)
    return {"ok": True, "stat_date": day.isoformat(), "return_days": return_days, "deleted": deleted}


@DFF.API("设置 RUM Bitmap 保留期", category="rum-retention", tags=["maintenance"], timeout=120)
def apply_bitmap_ttl(history_days=90):
    history_days = int(history_days)
    if history_days < 1 or history_days > BITMAP_RETENTION_DAYS:
        raise ValueError("history_days must be between 1 and %s" % BITMAP_RETENTION_DAYS)
    today = datetime.datetime.now(TIMEZONE).date()
    touched = 0
    for offset in range(history_days):
        day = today - datetime.timedelta(days=offset)
        for kind in (
            "active", "ordinary_new", "cost_center_new",
            "return_3d", "return_7d", "return_30d",
        ):
            key = _bitmap_key(kind, day)
            if int(_redis().query("EXISTS", key) or 0):
                _expire_daily_bitmap(key, day)
                touched += 1
    return {
        "ok": True,
        "history_days": history_days,
        "retention_days": BITMAP_RETENTION_DAYS,
        "keys_touched": touched,
    }


@DFF.API("检查 RUM 留存依赖", category="rum-retention", tags=["setup"], timeout=30)
def check_dependencies():
    db_rows = _mysql().query("SELECT 1 AS ok")
    redis = _redis()
    test_key = "rum:{%s}:selftest:%s" % (APP_ID, uuid.uuid4().hex)
    try:
        redis.query("SETBIT", test_key, 7, 1)
        bit = int(redis.query("GETBIT", test_key, 7) or 0)
        lua_result = int(redis.query("EVAL", "return redis.call('GETBIT',KEYS[1],ARGV[1])", 1, test_key, 7) or 0)
    finally:
        redis.query("DEL", test_key)
    return {
        "ok": bool(db_rows and db_rows[0].get("ok") == 1 and bit == 1 and lua_result == 1),
        "mysql": True,
        "redis": bit == 1 and lua_result == 1,
        "openapi_api_key_configured": bool(DFF.ENV("RUM_OPENAPI_API_KEY")),
        "openapi_url": DFF.ENV("RUM_OPENAPI_URL", default=OPENAPI_URL_DEFAULT),
    }


@DFF.API("初始化 RUM 留存数据表", category="rum-retention", tags=["setup"], timeout=120)
def init_schema():
    db = _mysql()
    for statement in SCHEMA_SQL:
        db.non_query_raw(statement)
    columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_user_identity")}
    if "user_name" not in columns:
        db.non_query_raw("ALTER TABLE rum_user_identity ADD COLUMN user_name VARCHAR(255) NULL AFTER email_normalized")
    columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_user_identity")}
    if "userid" not in columns:
        db.non_query_raw("ALTER TABLE rum_user_identity ADD COLUMN userid VARCHAR(128) NULL AFTER user_name")
    columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_user_identity")}
    if "gc_workspace_id" not in columns:
        db.non_query_raw("ALTER TABLE rum_user_identity ADD COLUMN gc_workspace_id VARCHAR(128) NULL AFTER userid")
    columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_user_identity")}
    if "gc_workspace_name" not in columns:
        db.non_query_raw("ALTER TABLE rum_user_identity ADD COLUMN gc_workspace_name VARCHAR(255) NULL AFTER gc_workspace_id")
    checkpoint_columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_collect_checkpoint")}
    if "invalid_email_count" not in checkpoint_columns:
        db.non_query_raw("ALTER TABLE rum_collect_checkpoint ADD COLUMN invalid_email_count BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER cost_center_candidate_count")
    checkpoint_columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_collect_checkpoint")}
    if "openapi_warning_count" not in checkpoint_columns:
        db.non_query_raw("ALTER TABLE rum_collect_checkpoint ADD COLUMN openapi_warning_count BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER invalid_email_count")
    checkpoint_columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_collect_checkpoint")}
    if "execution_duration_ms" not in checkpoint_columns:
        db.non_query_raw("ALTER TABLE rum_collect_checkpoint ADD COLUMN execution_duration_ms BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER openapi_warning_count")
    db.non_query(
        "INSERT INTO rum_bitmap_sequence (namespace_key, current_seq) VALUES (?, 0) ON DUPLICATE KEY UPDATE namespace_key=VALUES(namespace_key)",
        sql_params=[APP_ID],
    )
    return {"ok": True, "tables": 5, "app_id": APP_ID}


@DFF.API("采集指定日期 RUM 用户", category="rum-retention", tags=["collect"], timeout=3600)
def collect_day(stat_date, force=False):
    return _collect_one_day(_parse_date(stat_date), force=force)


@DFF.API("触发历史 RUM 用户回填", category="rum-retention", tags=["backfill"], timeout=30)
def trigger_backfill(days, end_date=None):
    days = int(days)
    if days < 1 or days > MAX_BACKFILL_DAYS:
        raise ValueError("days must be between 1 and %s" % MAX_BACKFILL_DAYS)
    end = _parse_date(end_date) if end_date else datetime.datetime.now(TIMEZONE).date() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=days - 1)
    job_id = str(uuid.uuid4())
    _mysql().non_query(
        "INSERT INTO rum_collect_job (job_id, job_type, start_date, end_date, status, total_days) VALUES (?, 'backfill', ?, ?, 'pending', ?)",
        sql_params=[job_id, start, end, days],
    )
    DFF.FUNC("run_backfill", kwargs={"job_id": job_id}, timeout=3600, expires=3600)
    return {"ok": True, "job_id": job_id, "status": "pending", "start_date": start.isoformat(), "end_date": end.isoformat(), "days": days}


@DFF.API("执行历史 RUM 用户回填", category="rum-retention", tags=["internal"], timeout=3600)
def run_backfill(job_id):
    db = _mysql()
    jobs = db.query(
        "SELECT start_date, end_date, total_days, status FROM rum_collect_job WHERE job_id=?",
        sql_params=[job_id],
    )
    if not jobs:
        raise ValueError("job not found")
    job = jobs[0]
    if job["status"] == "succeeded":
        return {"ok": True, "job_id": job_id, "status": "succeeded", "idempotent": True}
    start = _parse_date(job["start_date"])
    days = int(job["total_days"])
    db.non_query(
        "UPDATE rum_collect_job SET status='running', started_at=NOW(), error_message=NULL WHERE job_id=?",
        sql_params=[job_id],
    )
    results = []
    try:
        for index in range(days):
            day = start + datetime.timedelta(days=index)
            results.append(_collect_one_day(day))
            db.non_query(
                "UPDATE rum_collect_job SET completed_days=? WHERE job_id=?",
                sql_params=[index + 1, job_id],
            )
        db.non_query(
            "UPDATE rum_collect_job SET status='succeeded', result_json=?, finished_at=NOW() WHERE job_id=?",
            sql_params=[json.dumps(results, ensure_ascii=False), job_id],
        )
        return {"ok": True, "job_id": job_id, "status": "succeeded", "days": days}
    except Exception as exc:
        db.non_query(
            "UPDATE rum_collect_job SET status='failed', error_message=?, finished_at=NOW() WHERE job_id=?",
            sql_params=[str(exc)[:2000], job_id],
        )
        raise


@DFF.API("查询 RUM 采集任务", category="rum-retention", tags=["status"], timeout=30)
def get_job(job_id):
    rows = _mysql().query(
        "SELECT job_id, job_type, start_date, end_date, status, total_days, completed_days, result_json, error_message, created_at, started_at, finished_at FROM rum_collect_job WHERE job_id=?",
        sql_params=[job_id],
    )
    if not rows:
        return DFF.RESP({"ok": False, "error": "job not found"}, status_code=404)
    return {"ok": True, "job": rows[0]}


@DFF.API("每日 RUM 用户定时采集", category="rum-retention", tags=["cron"], timeout=3600)
def collect_yesterday():
    day = datetime.datetime.now(TIMEZONE).date() - datetime.timedelta(days=1)
    db = _mysql()
    daily = db.query(
        "SELECT status FROM rum_collect_day WHERE stat_date=?",
        sql_params=[day],
    )
    if daily and daily[0]["status"] == "succeeded":
        return {
            "ok": True,
            "status": "succeeded",
            "idempotent": True,
            "skipped": True,
            "date": day.isoformat(),
            "complete_source": "daily_rebuild",
        }
    start = datetime.datetime.combine(day, datetime.time.min)
    end = start + datetime.timedelta(days=1)
    checkpoints = db.query(
        "SELECT status, COUNT(*) count FROM rum_collect_checkpoint "
        "WHERE window_start>=? AND window_end<=? GROUP BY status",
        sql_params=[start, end],
    )
    counts = {row["status"]: int(row["count"]) for row in checkpoints}
    if counts.get("succeeded", 0) == 288:
        return {
            "ok": True,
            "status": "succeeded",
            "idempotent": True,
            "skipped": True,
            "date": day.isoformat(),
            "complete_source": "incremental",
            "succeeded_windows": 288,
        }
    return _collect_one_day(day)
