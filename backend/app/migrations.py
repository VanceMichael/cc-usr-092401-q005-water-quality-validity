"""SQLite 轻量启动迁移。

项目使用 Base.metadata.create_all 建表，不会自动给旧表加列，
这里补齐水质校验所需的新列，并把历史异常值隔离而不是静默丢弃。
"""

import json
from datetime import datetime, time

from sqlalchemy import inspect, text

from .database import SessionLocal, engine
from .models import WaterQualityRecord, WaterQualityRevision
from .validation import METRIC_KEYS, audit_metrics

# 新列定义：列名 -> (SQL 类型片段, 默认值 SQL)
_NEW_COLUMNS = {
    "sampled_at": ("DATETIME", None),
    "device_id": ("VARCHAR(50)", "'manual'"),
    "status": ("VARCHAR(20)", "'valid'"),
    "invalid_fields": ("TEXT", None),
    "review_conclusion": ("TEXT", None),
    "reviewed_by": ("VARCHAR(100)", None),
    "reviewed_at": ("DATETIME", None),
    "merged_into_id": ("INTEGER", None),
    "updated_at": ("DATETIME", None),
}

_REVISION_TABLE = "water_quality_revisions"


def _ensure_columns() -> None:
    inspector = inspect(engine)
    if "water_quality_records" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("water_quality_records")}
    with engine.begin() as conn:
        for name, (sql_type, default_sql) in _NEW_COLUMNS.items():
            if name in existing:
                continue
            fragment = f"ALTER TABLE water_quality_records ADD COLUMN {name} {sql_type}"
            if default_sql is not None:
                fragment += f" DEFAULT {default_sql}"
            conn.execute(text(fragment))
        # 补索引
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_water_quality_records_sampled_at "
            "ON water_quality_records (sampled_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_water_quality_records_device_id "
            "ON water_quality_records (device_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_water_quality_records_status "
            "ON water_quality_records (status)"
        ))


def _backfill_and_quarantine() -> None:
    db = SessionLocal()
    try:
        records = db.query(WaterQualityRecord).all()
        dirty = False
        for record in records:
            if not record.device_id:
                record.device_id = "manual"
                dirty = True
            if record.sampled_at is None and record.record_date is not None:
                record.sampled_at = datetime.combine(record.record_date, time.min)
                dirty = True
            if not record.status:
                record.status = "valid"
                dirty = True

            # 历史异常值：不删除，统一隔离并写明字段级原因，等待人工复核
            if record.status == "valid":
                values = {key: getattr(record, key) for key in METRIC_KEYS}
                reasons = audit_metrics(values)
                if reasons:
                    record.status = "quarantined"
                    record.invalid_fields = json.dumps(reasons, ensure_ascii=False)
                    record.review_conclusion = "系统升级时自动隔离的历史异常值，待人工复核"
                    record.reviewed_at = None
                    db.add(WaterQualityRevision(
                        record=record,
                        action="review",
                        changed_by="system-migration",
                        field_changes=json.dumps(
                            {"review": "auto_quarantine_legacy", "reasons": reasons},
                            ensure_ascii=False,
                        ),
                    ))
                    dirty = True
        if dirty:
            db.commit()
    finally:
        db.close()


def run_migrations() -> None:
    """在 Base.metadata.create_all 之后调用：补旧表列、回填、隔离历史异常值。"""
    _ensure_columns()
    _backfill_and_quarantine()
