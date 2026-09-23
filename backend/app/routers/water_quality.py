import csv
import io
import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Batch, WaterQualityRecord, WaterQualityCorrection
from ..schemas import (
    WaterQualityRecordCreate,
    WaterQualityRecordUpdate,
    WaterQualityRecordResponse,
    WaterQualityReviewRequest,
)
from ..validation import (
    METRIC_FIELDS,
    validate_metric,
    validate_water_quality_payload,
)

router = APIRouter(
    prefix="/api/water-quality-records",
    tags=["水质监测"]
)

# 时间 / 批次类字段（错误一律拒收，无法定位采样时刻的记录不得落库）
TIME_FIELDS = {"record_date", "record_time"}


def _field_error_response(errors: List[dict], status_code: int = 422):
    """统一字段级错误响应：前端按 errors[].field 在表单中定位。"""
    return JSONResponse(status_code=status_code, content={"errors": errors})


def _normalize_time(record_time: Optional[str]) -> str:
    if not record_time:
        return "00:00"
    return record_time.strip()[:5]


def _serialize(record: WaterQualityRecord) -> dict:
    try:
        original = json.loads(record.original_payload) if record.original_payload else None
    except (ValueError, TypeError):
        original = None
    return {
        "id": record.id,
        "batch_id": record.batch_id,
        "record_date": record.record_date,
        "record_time": record.record_time,
        "sample_time": record.sample_time,
        "water_temperature": record.water_temperature,
        "ph_value": record.ph_value,
        "dissolved_oxygen": record.dissolved_oxygen,
        "ammonia_nitrogen": record.ammonia_nitrogen,
        "nitrite": record.nitrite,
        "transparency": record.transparency,
        "notes": record.notes,
        "device_id": record.device_id or "manual",
        "source": record.source or "manual",
        "status": record.status,
        "quarantined_fields": record.quarantined_fields_list,
        "original_payload": original,
        "review_status": record.review_status,
        "review_note": record.review_note,
        "reviewed_by": record.reviewed_by,
        "reviewed_at": record.reviewed_at,
        "merged_into_id": record.merged_into_id,
        "client_ref": record.client_ref,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "corrections": [
            {
                "id": c.id,
                "record_id": c.record_id,
                "field_name": c.field_name,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "reason": c.reason,
                "operator": c.operator,
                "note": c.note,
                "created_at": c.created_at,
            }
            for c in (record.corrections or [])
        ],
    }


def _get_batch(db: Session, batch_id: int) -> Batch:
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


def _find_idempotent_record(
    db: Session, *, batch_id: int, record_date, sample_time: str,
    device_id: str, client_ref: Optional[str]
) -> Optional[WaterQualityRecord]:
    """同一设备同一次采样（或同一 client_ref）的主记录。"""
    if client_ref:
        existing = db.query(WaterQualityRecord).filter(
            WaterQualityRecord.device_id == device_id,
            WaterQualityRecord.client_ref == client_ref,
            WaterQualityRecord.merged_into_id.is_(None),
        ).first()
        if existing:
            return existing
    return db.query(WaterQualityRecord).filter(
        WaterQualityRecord.batch_id == batch_id,
        WaterQualityRecord.record_date == record_date,
        WaterQualityRecord.sample_time == sample_time,
        WaterQualityRecord.device_id == device_id,
        WaterQualityRecord.merged_into_id.is_(None),
    ).first()


def _payload_signature(record: WaterQualityRecord) -> dict:
    return {f: getattr(record, f) for f in METRIC_FIELDS}


@router.post("/", response_model=WaterQualityRecordResponse)
def create_water_quality_record(record: WaterQualityRecordCreate, db: Session = Depends(get_db)):
    batch = _get_batch(db, record.batch_id)

    data = record.model_dump(exclude_none=False)
    data["record_time"] = record.record_time or None
    clean, errors, raw_strings = validate_water_quality_payload(
        data,
        batch_stocking_date=batch.stocking_date,
        batch_actual_harvest_date=batch.actual_harvest_date,
    )

    device_id = (record.device_id or "manual").strip() or "manual"
    source = "sensor" if (record.device_id and device_id != "manual") else "manual"
    sample_time = _normalize_time(record.record_time)

    # 传感器自动上报：越界指标无法交互式修正，隔离留痕待人工复核；
    # 手工录入：任何字段错误一律 422，前端在表单中精确定位。
    time_errors = [e for e in errors if e["field"] in TIME_FIELDS]
    metric_errors = [e for e in errors if e["field"] not in TIME_FIELDS]
    if time_errors or (source == "manual" and metric_errors):
        return _field_error_response(errors)

    # 幂等：同一设备同一次采样重复上报 → 归并，不新建
    existing = _find_idempotent_record(
        db,
        batch_id=record.batch_id,
        record_date=record.record_date,
        sample_time=sample_time,
        device_id=device_id,
        client_ref=record.client_ref,
    )
    if existing:
        incoming_sig = {f: clean[f] for f in METRIC_FIELDS}
        if _payload_signature(existing) != incoming_sig:
            # 同键但数值冲突：先到为准，冲突报文留痕，可在更正历史中回看
            db.add(WaterQualityCorrection(
                record_id=existing.id,
                field_name="*",
                old_value=json.dumps(_payload_signature(existing), ensure_ascii=False, default=str),
                new_value=json.dumps(incoming_sig, ensure_ascii=False, default=str),
                reason="merged",
                operator=device_id,
                note="同一设备同一次采样的重复上报，数值与首报不一致，已归并并保留首报值",
            ))
            db.commit()
            db.refresh(existing)
        return _serialize(existing)

    quarantined = [e["field"] for e in metric_errors]
    new_record = WaterQualityRecord(
        batch_id=record.batch_id,
        record_date=record.record_date,
        record_time=record.record_time or None,
        sample_time=sample_time,
        notes=record.notes,
        device_id=device_id,
        source=source,
        status="quarantined" if quarantined else "valid",
        quarantined_fields_list=quarantined,
        original_payload=json.dumps(
            {**{f: raw_strings[f] for f in METRIC_FIELDS},
             "record_date": record.record_date.isoformat(),
             "record_time": record.record_time},
            ensure_ascii=False,
        ),
        client_ref=record.client_ref,
    )
    for field in METRIC_FIELDS:
        if field not in quarantined:
            setattr(new_record, field, clean[field])
        # 被隔离字段保持 NULL（缺测语义），原值已进 original_payload

    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return _serialize(new_record)


@router.get("/", response_model=List[WaterQualityRecordResponse])
def get_water_quality_records(
    skip: int = 0,
    limit: int = 1000,
    batch_id: int = None,
    status: str = None,
    include_merged: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(WaterQualityRecord)
    if batch_id:
        query = query.filter(WaterQualityRecord.batch_id == batch_id)
    if status:
        query = query.filter(WaterQualityRecord.status == status)
    if not include_merged:
        query = query.filter(WaterQualityRecord.merged_into_id.is_(None))
    query = query.order_by(
        WaterQualityRecord.record_date, WaterQualityRecord.sample_time, WaterQualityRecord.id
    )
    records = query.offset(skip).limit(limit).all()
    return [_serialize(r) for r in records]


@router.get("/export/")
def export_water_quality_records(batch_id: int = None, db: Session = Depends(get_db)):
    """导出 CSV：与接口、趋势图使用同一有效数据集（status=valid、未归并）。

    缺测导出为空字符串，真实 0 值导出为 0；隔离 / 被归并记录不出现在导出中。
    """
    query = db.query(WaterQualityRecord).filter(
        WaterQualityRecord.status == "valid",
        WaterQualityRecord.merged_into_id.is_(None),
    )
    if batch_id:
        query = query.filter(WaterQualityRecord.batch_id == batch_id)
    records = query.order_by(
        WaterQualityRecord.batch_id,
        WaterQualityRecord.record_date,
        WaterQualityRecord.sample_time,
    ).all()

    buffer = io.StringIO()
    buffer.write("﻿")  # UTF-8 BOM，便于 Excel 正确识别中文
    writer = csv.writer(buffer)
    writer.writerow([
        "批次ID", "检测日期", "检测时间",
        "水温(℃)", "pH值", "溶解氧(mg/L)",
        "氨氮(mg/L)", "亚硝酸盐(mg/L)", "透明度(cm)",
        "设备", "备注",
    ])
    for r in records:
        writer.writerow([
            r.batch_id,
            r.record_date.isoformat(),
            r.record_time or "",
            "" if r.water_temperature is None else r.water_temperature,
            "" if r.ph_value is None else r.ph_value,
            "" if r.dissolved_oxygen is None else r.dissolved_oxygen,
            "" if r.ammonia_nitrogen is None else r.ammonia_nitrogen,
            "" if r.nitrite is None else r.nitrite,
            "" if r.transparency is None else r.transparency,
            r.device_id or "manual",
            r.notes or "",
        ])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=water_quality_valid.csv"},
    )


@router.get("/{record_id}/", response_model=WaterQualityRecordResponse)
def get_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    return _serialize(record)


@router.put("/{record_id}/", response_model=WaterQualityRecordResponse)
def update_water_quality_record(
    record_id: int, record: WaterQualityRecordUpdate, db: Session = Depends(get_db)
):
    db_record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")

    update_data = record.model_dump(exclude_unset=True)

    # 合并后的影子记录不可修改
    if db_record.merged_into_id is not None:
        return _field_error_response(
            [{"field": "id", "message": "该记录已归并到其他记录，不可修改"}], status_code=409
        )

    # 以“修改后”的完整快照做时间 / 批次校验
    batch_id = update_data.get("batch_id", db_record.batch_id)
    batch = _get_batch(db, batch_id)
    merged = {
        "batch_id": batch_id,
        "record_date": update_data.get("record_date", db_record.record_date),
        "record_time": (update_data.get("record_time", db_record.record_time)
                        if "record_time" in update_data else db_record.record_time),
    }
    for field in METRIC_FIELDS:
        merged[field] = update_data[field] if field in update_data else getattr(db_record, field)

    _, errors, _ = validate_water_quality_payload(
        merged,
        batch_stocking_date=batch.stocking_date,
        batch_actual_harvest_date=batch.actual_harvest_date,
    )
    if errors:
        return _field_error_response(errors)

    new_sample_time = _normalize_time(
        update_data["record_time"] if "record_time" in update_data else db_record.record_time
    )
    # 幂等键冲突检查
    conflict = db.query(WaterQualityRecord).filter(
        WaterQualityRecord.id != record_id,
        WaterQualityRecord.batch_id == batch_id,
        WaterQualityRecord.record_date == merged["record_date"],
        WaterQualityRecord.sample_time == new_sample_time,
        WaterQualityRecord.device_id == db_record.device_id,
        WaterQualityRecord.merged_into_id.is_(None),
    ).first()
    if conflict:
        return _field_error_response(
            [{"field": "record_date",
              "message": "同一设备在该日期与时刻已存在采样记录，重复数据请走归并而非修改"}],
            status_code=409,
        )

    # 逐字段更正留痕（传感器原值 / 历次人工更正都可回看）
    try:
        original_snapshot = json.loads(db_record.original_payload) if db_record.original_payload else {}
    except (ValueError, TypeError):
        original_snapshot = {}
    for field in METRIC_FIELDS:
        if field not in update_data:
            continue
        old = getattr(db_record, field)
        new = update_data[field]
        was_quarantined = field in db_record.quarantined_fields_list
        # 隔离字段当前为 NULL，留痕时取化验员原始录入值
        old_for_audit = original_snapshot.get(field) if was_quarantined else old
        if old_for_audit == "":
            old_for_audit = None
        if old != new or was_quarantined:
            db.add(WaterQualityCorrection(
                record_id=db_record.id,
                field_name=field,
                old_value=None if old_for_audit is None else str(old_for_audit),
                new_value=None if new is None else str(new),
                reason="corrected",
                note="人工修改（隔离字段更正）" if was_quarantined else "人工修改",
            ))
        setattr(db_record, field, new)
        # 修改后若该字段有效，解除其隔离标记
        if was_quarantined and new is not None:
            db_record.quarantined_fields_list = [
                f for f in db_record.quarantined_fields_list if f != field
            ]

    if "record_time" in update_data:
        old_t = db_record.record_time
        new_t = update_data["record_time"]
        if old_t != new_t:
            db.add(WaterQualityCorrection(
                record_id=db_record.id, field_name="record_time",
                old_value=old_t, new_value=new_t, reason="corrected", note="人工修改采样时间",
            ))
        db_record.record_time = new_t
        db_record.sample_time = new_sample_time
    if "record_date" in update_data:
        old_d = db_record.record_date
        new_d = update_data["record_date"]
        if old_d != new_d:
            db.add(WaterQualityCorrection(
                record_id=db_record.id, field_name="record_date",
                old_value=str(old_d), new_value=str(new_d), reason="corrected", note="人工修改采样日期",
            ))
        db_record.record_date = new_d
    if "batch_id" in update_data:
        db_record.batch_id = batch_id
    if "notes" in update_data:
        db_record.notes = update_data["notes"]
    if "client_ref" in update_data:
        db_record.client_ref = update_data["client_ref"]

    # 所有隔离字段都被更正后恢复为有效记录
    if not db_record.quarantined_fields_list:
        db_record.status = "valid"

    db.commit()
    db.refresh(db_record)
    return _serialize(db_record)


@router.post("/{record_id}/review/", response_model=WaterQualityRecordResponse)
def review_water_quality_record(
    record_id: int, review: WaterQualityReviewRequest, db: Session = Depends(get_db)
):
    """隔离记录复核：确认异常 / 更正恢复 / 判定误报，均记录结论与留痕。"""
    record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    if record.merged_into_id is not None:
        return _field_error_response(
            [{"field": "id", "message": "该记录已归并，不能复核"}], status_code=409
        )

    record.review_status = review.review_status
    record.review_note = review.review_note
    record.reviewed_by = review.reviewed_by
    record.reviewed_at = datetime.utcnow()

    if review.review_status == "corrected":
        corrections = review.corrections or {}
        errors = []
        for field, value in corrections.items():
            if field not in METRIC_FIELDS:
                errors.append({"field": field, "message": "未知水质指标字段"})
                continue
            if value is not None:
                err = validate_metric(field, value)
                if err:
                    errors.append(err)
        if errors:
            db.rollback()
            return _field_error_response(errors)

        try:
            original = json.loads(record.original_payload) if record.original_payload else {}
        except (ValueError, TypeError):
            original = {}

        remaining = list(record.quarantined_fields_list)
        for field, value in corrections.items():
            old_display = original.get(field)
            old_current = getattr(record, field)
            if old_display is None:
                old_display = old_current
            if str(old_display) != str(value):
                db.add(WaterQualityCorrection(
                    record_id=record.id,
                    field_name=field,
                    old_value=None if old_display in (None, "") else str(old_display),
                    new_value=None if value is None else str(value),
                    reason="corrected",
                    operator=review.reviewed_by,
                    note=review.review_note or "隔离复核后更正",
                ))
            setattr(record, field, value)
            if field in remaining:
                remaining.remove(field)
        record.quarantined_fields_list = remaining
        if not remaining:
            record.status = "valid"
    else:
        # confirmed（异常属实）/ dismissed（误报）：保留隔离，原值与结论留痕
        db.add(WaterQualityCorrection(
            record_id=record.id,
            field_name="*",
            old_value=record.original_payload,
            new_value=None,
            reason=review.review_status,
            operator=review.reviewed_by,
            note=review.review_note,
        ))

    db.commit()
    db.refresh(record)
    return _serialize(record)


@router.delete("/{record_id}/")
def delete_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    db_record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    # 异常 / 待复核记录不得静默删除，必须先走复核流程
    if db_record.status == "quarantined" or db_record.merged_into_id is not None:
        return _field_error_response(
            [{"field": "id",
              "message": "隔离或已归并的记录不能删除，请先完成复核（更正 / 确认 / 判定误报）"}],
            status_code=409,
        )

    db.delete(db_record)
    db.commit()
    return {"message": "水质监测记录删除成功"}
