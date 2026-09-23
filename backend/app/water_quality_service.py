"""水质记录的共享业务逻辑。

接口列表、趋势图、CSV 导出与分析追溯都通过本模块取数，
确保它们使用完全一致的「有效数据集」：仅 status=valid、未被归并的记录。
"""

import json
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from sqlalchemy.orm import Session

from .models import Batch, WaterQualityRecord, WaterQualityRevision
from .validation import METRIC_KEYS, validate_payload


VALID_STATUS = "valid"
QUARANTINED_STATUS = "quarantined"
REJECTED_STATUS = "rejected"


def metric_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """从入参字典中抽取六项指标。"""
    return {key: data.get(key) for key in METRIC_KEYS}


def apply_metrics(record: WaterQualityRecord, values: Dict[str, Any]) -> None:
    for key in METRIC_KEYS:
        if key in values:
            value = values[key]
            setattr(record, key, None if value is None else float(value))


def record_snapshot(record: WaterQualityRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "batch_id": record.batch_id,
        "record_date": record.record_date.isoformat() if record.record_date else None,
        "record_time": record.record_time,
        "sampled_at": record.sampled_at.isoformat() if record.sampled_at else None,
        "device_id": record.device_id,
        **{key: getattr(record, key) for key in METRIC_KEYS},
        "status": record.status,
        "notes": record.notes,
    }


def add_revision(
    db: Session,
    record: WaterQualityRecord,
    *,
    action: str,
    changed_by: str,
    field_changes: Dict[str, Any],
) -> None:
    db.add(
        WaterQualityRevision(
            record=record,
            action=action,
            changed_by=changed_by,
            field_changes=json.dumps(field_changes, ensure_ascii=False, default=str),
            snapshot=json.dumps(record_snapshot(record), ensure_ascii=False, default=str),
        )
    )


def validate_against_batch(
    batch: Optional[Batch],
    data: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    enforce_upload_window: bool = True,
):
    """对一组入参执行完整校验，返回 (sampled_at, 硬性错误, 可隔离错误)。"""
    return validate_payload(
        batch=batch,
        record_date=data.get("record_date"),
        record_time=data.get("record_time"),
        metric_values=metric_payload(data),
        now=now,
        enforce_upload_window=enforce_upload_window,
    )


def find_duplicate(
    db: Session,
    *,
    batch_id: int,
    device_id: str,
    sampled_at: datetime,
    exclude_id: Optional[int] = None,
) -> Optional[WaterQualityRecord]:
    """幂等键：同一设备 + 同一批次 + 同一采样时刻。"""
    query = db.query(WaterQualityRecord).filter(
        WaterQualityRecord.batch_id == batch_id,
        WaterQualityRecord.device_id == device_id,
        WaterQualityRecord.sampled_at == sampled_at,
        WaterQualityRecord.merged_into_id.is_(None),
    )
    if exclude_id is not None:
        query = query.filter(WaterQualityRecord.id != exclude_id)
    return query.first()


def valid_query(db: Session, batch_id: Optional[int] = None):
    """所有消费端共用的有效数据集查询：valid 且未被归并。"""
    query = db.query(WaterQualityRecord).filter(
        WaterQualityRecord.status == VALID_STATUS,
        WaterQualityRecord.merged_into_id.is_(None),
    )
    if batch_id is not None:
        query = query.filter(WaterQualityRecord.batch_id == batch_id)
    return query


def get_valid_records(
    db: Session, batch_id: Optional[int] = None
) -> Iterable[WaterQualityRecord]:
    return valid_query(db, batch_id).order_by(
        WaterQualityRecord.sampled_at.asc(), WaterQualityRecord.id.asc()
    ).all()


def get_batch_or_none(db: Session, batch_id: Any) -> Optional[Batch]:
    try:
        bid = int(batch_id)
    except (TypeError, ValueError):
        return None
    return db.query(Batch).filter(Batch.id == bid).first()
