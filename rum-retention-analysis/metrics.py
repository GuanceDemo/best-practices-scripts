import datetime
import hashlib
import json
import uuid

APP_ID = "cn1_guance_com"
MYSQL_CONNECTOR_ID = "rum_mysql"
REDIS_CONNECTOR_ID = "rum_redis"
DATAWAY_CONNECTOR_ID = "demo_dataway"
TIMEZONE = datetime.timezone(datetime.timedelta(hours=8))
CALCULATION_VERSION = "v5"

METRIC_TABLE_SQL = """CREATE TABLE IF NOT EXISTS rum_metric_result (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    measurement VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    calculation_date DATE NOT NULL,
    cohort_date DATE NULL,
    metric_timestamp BIGINT UNSIGNED NOT NULL,
    dimension_key VARCHAR(255) NOT NULL DEFAULT '',
    dimension_json JSON NOT NULL,
    fields_json JSON NOT NULL,
    calculation_version VARCHAR(32) NOT NULL,
    calculate_status VARCHAR(16) NOT NULL,
    write_status VARCHAR(16) NOT NULL,
    write_attempts INT UNSIGNED NOT NULL DEFAULT 0,
    last_write_code INT NULL,
    last_write_result VARCHAR(2000) NULL,
    calculated_at DATETIME NOT NULL,
    written_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_metric_result (measurement, stat_date, dimension_key, calculation_version),
    KEY idx_write_status (write_status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def _mysql():
    return DFF.CONN(MYSQL_CONNECTOR_ID)


def _redis():
    return DFF.CONN(REDIS_CONNECTOR_ID)


def _dataway():
    return DFF.CONN(DATAWAY_CONNECTOR_ID)


def _parse_date(value):
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()


def _yesterday():
    return datetime.datetime.now(TIMEZONE).date() - datetime.timedelta(days=1)


def _metric_timestamp(day):
    dt = datetime.datetime.combine(day, datetime.time(23, 59, 59), tzinfo=TIMEZONE)
    return int(dt.timestamp())


def _bitmap_key(kind, day):
    return "rum:{%s}:%s:%s" % (APP_ID, kind, day.isoformat())


def _tmp_key(prefix):
    return "rum:{%s}:tmp:metrics:%s:%s" % (APP_ID, prefix, uuid.uuid4().hex)


def _bitcount(key):
    return int(_redis().query("BITCOUNT", key) or 0)


def _bitop(operation, keys):
    target = _tmp_key(operation.lower())
    redis = _redis()
    try:
        redis.query("BITOP", operation, target, *keys)
        return target, int(redis.query("BITCOUNT", target) or 0)
    except Exception:
        redis.query("DEL", target)
        raise


def _intersection_count(keys):
    target, count = _bitop("AND", keys)
    _redis().query("DEL", target)
    return count


def _union_count(keys):
    target, count = _bitop("OR", keys)
    _redis().query("DEL", target)
    return count


def _dimension_key(tags):
    return "|".join("%s=%s" % (key, tags[key]) for key in sorted(tags))


def _is_day_complete(day):
    db = _mysql()
    daily = db.query(
        "SELECT status FROM rum_collect_day WHERE stat_date=?",
        sql_params=[day],
    )
    if daily and daily[0]["status"] == "succeeded":
        return True, "daily_rebuild"
    start = datetime.datetime.combine(day, datetime.time.min)
    end = start + datetime.timedelta(days=1)
    rows = db.query(
        "SELECT status, COUNT(*) count FROM rum_collect_checkpoint WHERE window_start>=? AND window_end<=? GROUP BY status",
        sql_params=[start, end],
    )
    counts = {row["status"]: int(row["count"]) for row in rows}
    return counts.get("succeeded", 0) == 288, "incremental"


def _quality_fields(day):
    db = _mysql()
    start = datetime.datetime.combine(day, datetime.time.min)
    end = start + datetime.timedelta(days=1)
    rows = db.query(
        "SELECT status, COUNT(*) count FROM rum_collect_checkpoint WHERE window_start>=? AND window_end<=? GROUP BY status",
        sql_params=[start, end],
    )
    counts = {row["status"]: int(row["count"]) for row in rows}
    daily = db.query(
        "SELECT status, active_count, ordinary_first_count, cost_center_first_count, invalid_email_count, warning_json FROM rum_collect_day WHERE stat_date=?",
        sql_params=[day],
    )
    succeeded = counts.get("succeeded", 0)
    failed = counts.get("failed", 0)
    expected = 288
    missing = max(0, expected - succeeded - failed)
    full, source = _is_day_complete(day)
    row = daily[0] if daily else {}
    warning_json = row.get("warning_json")
    if isinstance(warning_json, str):
        try:
            warning_count = len(json.loads(warning_json) or [])
        except Exception:
            warning_count = 0
    elif isinstance(warning_json, list):
        warning_count = len(warning_json)
    else:
        warning_count = 0
    return {
        "expected_window_count": expected,
        "succeeded_window_count": succeeded,
        "failed_window_count": failed,
        "missing_window_count": missing,
        "window_completion_rate": float(succeeded) / expected,
        "daily_data_complete": 1 if full else 0,
        "daily_active_users": int(row.get("active_count") or 0),
        "daily_ordinary_new_users": int(row.get("ordinary_first_count") or 0),
        "daily_cost_center_new_users": int(row.get("cost_center_first_count") or 0),
        "invalid_email_count": int(row.get("invalid_email_count") or 0),
        "openapi_warning_count": warning_count,
        "daily_calculation_success": 1,
    }, source


def _daily_fields(day):
    active_keys = [_bitmap_key("active", day - datetime.timedelta(days=i)) for i in range(7)]
    consecutive_3d_keys = [_bitmap_key("active", day - datetime.timedelta(days=i)) for i in range(3)]
    return {
        "dau": _bitcount(_bitmap_key("active", day)),
        "rolling_7d_wau": _union_count(active_keys),
        "consecutive_3d_users": _intersection_count(consecutive_3d_keys),
        "ordinary_new_users": _bitcount(_bitmap_key("ordinary_new", day)),
        "cost_center_new_users": _bitcount(_bitmap_key("cost_center_new", day)),
    }


def _retention_result(cohort_day, cohort_type, days):
    cohort_kind = "ordinary_new" if cohort_type == "ordinary" else "active"
    keys = [_bitmap_key(cohort_kind, cohort_day)]
    for offset in range(1, days + 1):
        keys.append(_bitmap_key("active", cohort_day + datetime.timedelta(days=offset)))
    cohort_users = _bitcount(keys[0])
    retained_users = _intersection_count(keys)
    return {
        "cohort_users": cohort_users,
        "retained_users": retained_users,
        "retention_rate": float(retained_users) / cohort_users if cohort_users else 0.0,
    }


def _lifecycle_fields(day):
    inactive_3d_before = day - datetime.timedelta(days=2)
    inactive_5d_before = day - datetime.timedelta(days=4)
    rows = _mysql().query(
        """SELECT COUNT(*) AS total_users,
        COALESCE(SUM(CASE WHEN last_active_date < ? THEN 1 ELSE 0 END), 0) AS inactive_3d_users,
        COALESCE(SUM(CASE WHEN last_active_date < ? THEN 1 ELSE 0 END), 0) AS inactive_5d_users
        FROM rum_user_identity
        WHERE first_active_date IS NOT NULL AND first_active_date <= ?""",
        sql_params=[inactive_3d_before, inactive_5d_before, day],
    )
    row = rows[0] if rows else {}
    return {
        "total_users": int(row.get("total_users") or 0),
        "inactive_3d_users": int(row.get("inactive_3d_users") or 0),
        "inactive_5d_users": int(row.get("inactive_5d_users") or 0),
        "return_3d_users": _bitcount(_bitmap_key("return_3d", day)),
        "return_7d_users": _bitcount(_bitmap_key("return_7d", day)),
        "return_30d_users": _bitcount(_bitmap_key("return_30d", day)),
    }


def _archive(measurement, calculation_date, tags, fields, cohort_date=None):
    dimension_key = _dimension_key(tags)
    timestamp = _metric_timestamp(calculation_date)
    db = _mysql()
    db.non_query(
        """INSERT INTO rum_metric_result
        (measurement, stat_date, calculation_date, cohort_date, metric_timestamp,
         dimension_key, dimension_json, fields_json, calculation_version,
         calculate_status, write_status, calculated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', 'pending', NOW())
        ON DUPLICATE KEY UPDATE calculation_date=VALUES(calculation_date),
        cohort_date=VALUES(cohort_date), metric_timestamp=VALUES(metric_timestamp),
        dimension_json=VALUES(dimension_json), fields_json=VALUES(fields_json),
        calculate_status='succeeded', write_status='pending', calculated_at=NOW(),
        last_write_code=NULL, last_write_result=NULL""",
        sql_params=[
            measurement, calculation_date, calculation_date, cohort_date, timestamp,
            dimension_key, json.dumps(tags, ensure_ascii=False),
            json.dumps(fields, ensure_ascii=False), CALCULATION_VERSION,
        ],
    )


def _calculate(day):
    complete, complete_source = _is_day_complete(day)
    quality, quality_source = _quality_fields(day)
    quality["daily_data_complete"] = 1 if complete else 0
    _archive(
        "rum_business_data_quality", day,
        {"granularity": "daily", "job_type": "daily_metric"},
        quality,
    )
    if not complete:
        raise RuntimeError("daily data is incomplete for %s" % day)

    _archive("rum_business_daily", day, {}, _daily_fields(day))

    for retention_days in (1, 3):
        cohort_day = day - datetime.timedelta(days=retention_days)
        required_days = [cohort_day + datetime.timedelta(days=i) for i in range(retention_days + 1)]
        if not all(_is_day_complete(required)[0] for required in required_days):
            continue
        for cohort_type in ("ordinary", "active"):
            tags = {
                "cohort_type": cohort_type,
                "retention_type": "consecutive",
                "retention_days": str(retention_days),
            }
            _archive(
                "rum_business_retention", day, tags,
                _retention_result(cohort_day, cohort_type, retention_days),
                cohort_date=cohort_day,
            )

    _archive(
        "rum_business_lifecycle", day,
        {},
        _lifecycle_fields(day),
    )
    return {"ok": True, "stat_date": day.isoformat(), "complete_source": complete_source}


def _pending_rows(stat_date=None, limit=100):
    sql = """SELECT id, measurement, stat_date, metric_timestamp, dimension_json,
             fields_json, calculation_version FROM rum_metric_result
             WHERE write_status IN ('pending','failed')"""
    params = []
    if stat_date:
        sql += " AND stat_date=?"
        params.append(stat_date)
    sql += " ORDER BY stat_date, id LIMIT ?"
    params.append(int(limit))
    return _mysql().query(sql, sql_params=params)


def _decode_json(value):
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value or "{}")


def _write_rows(rows):
    if not rows:
        return {"ok": True, "point_count": 0}
    points = []
    for row in rows:
        points.append({
            "measurement": row["measurement"],
            "tags": {str(k): str(v) for k, v in _decode_json(row["dimension_json"]).items()},
            "fields": _decode_json(row["fields_json"]),
            "timestamp": int(row["metric_timestamp"]),
        })
    ids = [int(row["id"]) for row in rows]
    db = _mysql()
    db.non_query(
        "UPDATE rum_metric_result SET write_status='writing', write_attempts=write_attempts+1 WHERE id IN (?)",
        sql_params=[ids],
    )
    try:
        status_code, result = _dataway().write_by_category_many(category="metric", data=points)
        success = 200 <= int(status_code) < 300
        if not success:
            raise RuntimeError("DataWay HTTP %s: %s" % (status_code, str(result)[:1000]))
        db.non_query(
            "UPDATE rum_metric_result SET write_status='succeeded', last_write_code=?, last_write_result=?, written_at=NOW() WHERE id IN (?)",
            sql_params=[int(status_code), str(result)[:2000], ids],
        )
        return {"ok": True, "point_count": len(points), "status_code": int(status_code), "result": result}
    except Exception as exc:
        db.non_query(
            "UPDATE rum_metric_result SET write_status='failed', last_write_result=? WHERE id IN (?)",
            sql_params=[str(exc)[:2000], ids],
        )
        raise


@DFF.API("写入 RUM 窗口质量指标", category="rum-retention", tags=["quality"], timeout=120)
def write_window_quality(window_start, window_end):
    start = datetime.datetime.fromisoformat(window_start)
    end = datetime.datetime.fromisoformat(window_end)
    start_naive = start.replace(tzinfo=None)
    end_naive = end.replace(tzinfo=None)
    rows = _mysql().query(
        """SELECT status, active_count, ordinary_candidate_count, cost_center_candidate_count,
        invalid_email_count, openapi_warning_count, execution_duration_ms
        FROM rum_collect_checkpoint WHERE window_start=? AND window_end=?""",
        sql_params=[start_naive, end_naive],
    )
    if not rows:
        raise ValueError("checkpoint not found")
    row = rows[0]
    fields = {
        "window_success": 1 if row["status"] == "succeeded" else 0,
        "window_delay_seconds": max(0, int((datetime.datetime.now(TIMEZONE) - end.astimezone(TIMEZONE)).total_seconds())),
        "window_active_users": int(row.get("active_count") or 0),
        "window_ordinary_new_users": int(row.get("ordinary_candidate_count") or 0),
        "window_cost_center_new_users": int(row.get("cost_center_candidate_count") or 0),
        "invalid_email_count": int(row.get("invalid_email_count") or 0),
        "openapi_warning_count": int(row.get("openapi_warning_count") or 0),
        "execution_duration_ms": int(row.get("execution_duration_ms") or 0),
    }
    status_code, result = _dataway().write_by_category(
        category="metric", measurement="rum_business_data_quality",
        tags={"granularity": "window", "job_type": "incremental"},
        fields=fields, timestamp=int(end.timestamp()),
    )
    if not 200 <= int(status_code) < 300:
        raise RuntimeError("DataWay HTTP %s: %s" % (status_code, str(result)[:1000]))
    return {"ok": True, "status_code": int(status_code), "fields": fields}


@DFF.API("初始化 RUM 业务指标表", category="rum-retention", tags=["metrics"], timeout=60)
def init_metric_schema():
    db = _mysql()
    db.non_query_raw(METRIC_TABLE_SQL)
    columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_metric_result")}
    if "calculation_date" not in columns:
        db.non_query_raw("ALTER TABLE rum_metric_result ADD COLUMN calculation_date DATE NULL AFTER stat_date")
        db.non_query_raw("UPDATE rum_metric_result SET calculation_date=stat_date WHERE calculation_date IS NULL")
        db.non_query_raw("ALTER TABLE rum_metric_result MODIFY COLUMN calculation_date DATE NOT NULL")
    columns = {row["Field"] for row in db.query_raw("SHOW COLUMNS FROM rum_metric_result")}
    if "cohort_date" not in columns:
        db.non_query_raw("ALTER TABLE rum_metric_result ADD COLUMN cohort_date DATE NULL AFTER calculation_date")
    indexes = db.query_raw("SHOW INDEX FROM rum_user_identity WHERE Key_name='idx_lifecycle_dates'")
    if not indexes:
        db.non_query_raw("ALTER TABLE rum_user_identity ADD INDEX idx_lifecycle_dates (first_active_date, last_active_date)")
    return {
        "ok": True,
        "table": "rum_metric_result",
        "calculation_date": True,
        "cohort_date": True,
        "lifecycle_index": True,
    }


@DFF.API("预览 RUM 业务指标", category="rum-retention", tags=["metrics"], timeout=300)
def preview_daily_metrics(stat_date):
    day = _parse_date(stat_date)
    complete, source = _is_day_complete(day)
    output = {
        "stat_date": day.isoformat(),
        "data_complete": complete,
        "complete_source": source,
        "quality": _quality_fields(day)[0],
    }
    if complete:
        output["daily"] = _daily_fields(day)
        output["lifecycle"] = _lifecycle_fields(day)
        output["retention"] = []
        for days in (1, 3):
            cohort_day = day - datetime.timedelta(days=days)
            for cohort_type in ("ordinary", "active"):
                output["retention"].append({
                    "cohort_date": cohort_day.isoformat(),
                    "cohort_type": cohort_type,
                    "retention_days": days,
                    "fields": _retention_result(cohort_day, cohort_type, days),
                })
    return output


@DFF.API("计算并写回 RUM 每日业务指标", category="rum-retention", tags=["metrics"], timeout=300)
def calculate_and_write_daily(stat_date=None, dry_run=False):
    day = _parse_date(stat_date) if stat_date else _yesterday()
    if dry_run:
        return preview_daily_metrics(day.isoformat())
    calculation = _calculate(day)
    write_result = _write_rows(_pending_rows(limit=100))
    return {"calculation": calculation, "write": write_result}


@DFF.API("重试 RUM 指标写回", category="rum-retention", tags=["metrics"], timeout=300)
def retry_metric_writes(limit=100):
    return _write_rows(_pending_rows(limit=int(limit)))
