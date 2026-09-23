"""SQLite 轻量幂等迁移。

项目未引入 Alembic；``Base.metadata.create_all`` 只建新表、不会给旧表加列。
这里在启动时检查信息架构（inspector），对缺失的列 / 索引做幂等补齐，
并把历史脏数据（pH=90、溶氧=Infinity 等）**隔离而不是删除**：
原值快照写入 original_payload，异常字段记入 quarantined_fields 后置空，
记录状态改为 quarantined 等待复核。
"""

import json
import math

from sqlalchemy import inspect, text

from .validation import METRIC_FIELDS, METRIC_SPECS

_NEW_COLUMNS = {
    "water_quality_records": [
        ("sample_time", "VARCHAR(5) NOT NULL DEFAULT '00:00'"),
        ("device_id", "VARCHAR(64) NOT NULL DEFAULT 'manual'"),
        ("source", "VARCHAR(20) NOT NULL DEFAULT 'manual'"),
        ("status", "VARCHAR(20) NOT NULL DEFAULT 'valid'"),
        ("quarantined_fields", "TEXT DEFAULT '[]'"),
        ("original_payload", "TEXT"),
        ("review_status", "VARCHAR(20)"),
        ("review_note", "TEXT"),
        ("reviewed_by", "VARCHAR(100)"),
        ("reviewed_at", "DATETIME"),
        ("merged_into_id", "INTEGER"),
        ("client_ref", "VARCHAR(100)"),
        ("updated_at", "DATETIME"),
    ],
}


def _is_invalid(value) -> bool:
    if value is None:
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(numeric):
        return True
    return False


def _backfill_legacy_anomalies(conn) -> None:
    """扫描旧记录，越界 / 非有限值隔离留痕。"""
    rows = conn.execute(
        text(
            "SELECT id, water_temperature, ph_value, dissolved_oxygen, "
            "ammonia_nitrogen, nitrite, transparency, status, original_payload "
            "FROM water_quality_records"
        )
    ).mappings().all()

    for row in rows:
        bad_fields = []
        snapshot = {}
        for field in METRIC_FIELDS:
            value = row[field]
            snapshot[field] = value
            if value is None:
                continue
            spec = METRIC_SPECS[field]
            if _is_invalid(value) or not (spec["min"] <= float(value) <= spec["max"]):
                bad_fields.append(field)

        if not bad_fields:
            continue

        # 已经处理过的记录（有快照且字段清单一致）不重复处理
        existing_q = row.get("original_payload")
        assignments = {
            "quarantined_fields": json.dumps(bad_fields, ensure_ascii=False),
            "status": "quarantined",
        }
        if not existing_q:
            assignments["original_payload"] = json.dumps(snapshot, ensure_ascii=False, default=str)
        for field in bad_fields:
            assignments[field] = None  # 隔离：不参与有效数据集；原值已在快照中

        set_clause = ", ".join(f"{k} = :{k}" for k in assignments)
        assignments["rid"] = row["id"]
        conn.execute(
            text(f"UPDATE water_quality_records SET {set_clause} WHERE id = :rid"),
            assignments,
        )


def _mark_legacy_duplicates(conn) -> None:
    """旧库中同一批次/日期/时刻/设备的重复行：保留最新一条，其余标记归并。"""
    dupes = conn.execute(
        text(
            "SELECT id, batch_id, record_date, sample_time, device_id FROM ("
            "  SELECT id, batch_id, record_date, sample_time, device_id, "
            "    ROW_NUMBER() OVER ("
            "      PARTITION BY batch_id, record_date, sample_time, device_id "
            "      ORDER BY id DESC) AS rn"
            "  FROM water_quality_records WHERE merged_into_id IS NULL"
            ") WHERE rn > 1"
        )
    ).all()
    for dup in dupes:
        keep = conn.execute(
            text(
                "SELECT id FROM water_quality_records "
                "WHERE batch_id = :b AND record_date = :d AND sample_time = :t "
                "AND device_id = :dev AND merged_into_id IS NULL "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"b": dup[1], "d": dup[2], "t": dup[3], "dev": dup[4]},
        ).scalar()
        if keep and keep != dup[0]:
            conn.execute(
                text("UPDATE water_quality_records SET merged_into_id = :k WHERE id = :i"),
                {"k": keep, "i": dup[0]},
            )


def run_lightweight_migrations(engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    table = "water_quality_records"
    if table not in tables:
        return  # 全新库由 create_all 建表，结构已齐全

    with engine.begin() as conn:
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, ddl in _NEW_COLUMNS[table]:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

        # 回填采样时刻（ADD COLUMN 的默认值会让旧行得到 '00:00'，
        # 因此这里必须无条件按 record_time 重算，不能加 WHERE 过滤）
        conn.execute(
            text(
                "UPDATE water_quality_records "
                "SET sample_time = CASE "
                "  WHEN record_time IS NULL OR record_time = '' THEN '00:00' "
                "  ELSE substr(record_time, 1, 5) END"
            )
        )

        _backfill_legacy_anomalies(conn)
        _mark_legacy_duplicates(conn)

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_wq_status "
                         "ON water_quality_records (status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_wq_client_ref "
                         "ON water_quality_records (status, client_ref)"))
        # 部分唯一索引：已归并的历史重复行不参与约束，新写入据此幂等。
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_wq_sample_device "
            "ON water_quality_records (batch_id, record_date, sample_time, device_id) "
            "WHERE merged_into_id IS NULL"
        ))
