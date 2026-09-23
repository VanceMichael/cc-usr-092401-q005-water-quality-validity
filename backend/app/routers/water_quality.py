import csv
import io
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import WaterQualityRecord, WaterQualityRevision
from ..schemas import (
    WaterQualityRecordCreate,
    WaterQualityRecordUpdate,
    WaterQualityRecordResponse,
    WaterQualityRecordCreateResponse,
    WaterQualityReviewRequest,
    WaterQualityTrendResponse,
    WaterQualityTrendPoint,
)
from ..validation import METRIC_KEYS, METRIC_SPECS, validate_metric
from ..water_quality_service import (
    VALID_STATUS,
    QUARANTINED_STATUS,
    REJECTED_STATUS,
    add_revision,
    apply_metrics,
    find_duplicate,
    get_batch_or_none,
    get_valid_records,
    metric_payload,
    validate_against_batch,
    valid_query,
)

router = APIRouter(
    prefix="/api/water-quality-records",
    tags=["水质监测"]
)


def _error_response(status_code: int, field_errors: dict, message: str = "数据校验未通过"):
    return HTTPException(
        status_code=status_code,
        detail={"message": message, "field_errors": field_errors},
    )


def _pydantic_field_errors(exc: ValidationError) -> dict:
    errors = {}
    for err in exc.errors():
        loc = [part for part in err.get("loc", []) if part != "__root__"]
        key = str(loc[-1]) if loc else "form"
        errors.setdefault(key, f"字段格式有误：{err.get('msg', '请检查输入')}")
    return errors


def serialize_record(record: WaterQualityRecord) -> dict:
    invalid_reasons = None
    if record.invalid_fields:
        try:
            invalid_reasons = json.loads(record.invalid_fields)
        except (ValueError, TypeError):
            invalid_reasons = {"form": record.invalid_fields}
    return {
        "id": record.id,
        "batch_id": record.batch_id,
        "record_date": record.record_date,
        "record_time": record.record_time,
        "device_id": record.device_id or "manual",
        "sampled_at": record.sampled_at,
        **{key: getattr(record, key) for key in METRIC_KEYS},
        "notes": record.notes,
        "status": record.status,
        "invalid_field_reasons": invalid_reasons,
        "review_conclusion": record.review_conclusion,
        "reviewed_by": record.reviewed_by,
        "reviewed_at": record.reviewed_at,
        "merged_into_id": record.merged_into_id,
        "revisions": [
            {
                "id": rev.id,
                "action": rev.action,
                "changed_by": rev.changed_by,
                "field_changes": json.loads(rev.field_changes) if rev.field_changes else None,
                "created_at": rev.created_at,
            }
            for rev in record.revisions
        ],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


@router.post("/", response_model=WaterQualityRecordCreateResponse, status_code=201)
async def create_water_quality_record(
    request: Request,
    accept_quarantine: bool = False,
    db: Session = Depends(get_db),
):
    try:
        raw = await request.json()
    except Exception:
        raise _error_response(400, {"form": "请求体必须是合法的 JSON"})

    try:
        payload = WaterQualityRecordCreate(**(raw if isinstance(raw, dict) else {}))
    except ValidationError as exc:
        raise _error_response(400, _pydantic_field_errors(exc))

    data = payload.model_dump()
    device_id = (data.get("device_id") or "manual").strip() or "manual"
    data["device_id"] = device_id

    db_batch = get_batch_or_none(db, data["batch_id"])
    if not db_batch:
        raise _error_response(404, {"batch_id": "批次不存在"})

    sampled_at, hard_errors, soft_errors = validate_against_batch(db_batch, data)

    # 非有限数等硬性错误一律拒绝，不允许保存或隔离；同时附上越界等提示，
    # 让表单一次性定位全部问题字段
    if hard_errors:
        raise _error_response(400, {**soft_errors, **hard_errors},
                              message="存在必须修正后才能提交的字段")
    # 越界/时间窗口/批次有效期问题默认拒绝；显式 accept_quarantine 时隔离待复核
    if soft_errors and not accept_quarantine:
        raise _error_response(400, soft_errors)

    # 同一设备同一采样时刻的重复数据：幂等归并到已有主记录，不产生第二行
    existing = find_duplicate(
        db,
        batch_id=data["batch_id"],
        device_id=device_id,
        sampled_at=sampled_at,
    )
    if existing is not None:
        changes = {}
        for key in METRIC_KEYS:
            new_value = data.get(key)
            if new_value is not None and getattr(existing, key) != new_value:
                changes[key] = {"old": getattr(existing, key), "new": new_value}
                setattr(existing, key, new_value)
        if data.get("notes") and data["notes"] != existing.notes:
            changes["notes"] = {"old": existing.notes, "new": data["notes"]}
            existing.notes = data["notes"]
        add_revision(
            db,
            existing,
            action="merge",
            changed_by=device_id,
            field_changes=changes or {"info": "重复提交，按幂等处理，数据未变化"},
        )
        db.commit()
        db.refresh(existing)
        result = serialize_record(existing)
        result["merged_from_duplicate"] = True
        return result

    new_record = WaterQualityRecord(
        batch_id=data["batch_id"],
        record_date=data["record_date"],
        record_time=data["record_time"],
        sampled_at=sampled_at,
        device_id=device_id,
        notes=data.get("notes"),
        status=QUARANTINED_STATUS if soft_errors else VALID_STATUS,
        invalid_fields=json.dumps(soft_errors, ensure_ascii=False) if soft_errors else None,
    )
    apply_metrics(new_record, metric_payload(data))
    db.add(new_record)
    db.flush()
    add_revision(
        db,
        new_record,
        action="create",
        changed_by=device_id,
        field_changes={"source": "sensor" if device_id != "manual" else "manual_entry"},
    )
    db.commit()
    db.refresh(new_record)
    result = serialize_record(new_record)
    result["merged_from_duplicate"] = False
    return result


@router.get("/", response_model=list[WaterQualityRecordResponse])
def get_water_quality_records(
    skip: int = 0,
    limit: int = 1000,
    batch_id: int = None,
    status: str = "valid",
    include_merged: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(WaterQualityRecord)
    if batch_id:
        query = query.filter(WaterQualityRecord.batch_id == batch_id)
    if status != "all":
        if status not in (VALID_STATUS, QUARANTINED_STATUS, REJECTED_STATUS):
            raise _error_response(400, {"status": "status 只能取 valid/quarantined/rejected/all"})
        query = query.filter(WaterQualityRecord.status == status)
    if not include_merged:
        query = query.filter(WaterQualityRecord.merged_into_id.is_(None))
    records = (
        query.order_by(WaterQualityRecord.record_date.desc(), WaterQualityRecord.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [serialize_record(r) for r in records]


@router.get("/trend/", response_model=WaterQualityTrendResponse)
def get_water_quality_trend(
    batch_id: int = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    db: Session = Depends(get_db),
):
    """趋势图数据源：只包含有效（valid、未归并）记录，与导出、分析共用同一数据集。"""
    query = valid_query(db, batch_id)
    if start:
        query = query.filter(WaterQualityRecord.sampled_at >= start)
    if end:
        query = query.filter(WaterQualityRecord.sampled_at <= end)
    records = query.order_by(
        WaterQualityRecord.sampled_at.asc(), WaterQualityRecord.id.asc()
    ).all()

    points = [
        WaterQualityTrendPoint(
            record_id=r.id,
            sampled_at=r.sampled_at,
            record_date=r.record_date,
            record_time=r.record_time,
            device_id=r.device_id,
            **{key: getattr(r, key) for key in METRIC_KEYS},
        )
        for r in records
    ]

    base_q = db.query(WaterQualityRecord).filter(WaterQualityRecord.merged_into_id.is_(None))
    if batch_id:
        base_q = base_q.filter(WaterQualityRecord.batch_id == batch_id)
    quarantined_count = base_q.filter(
        WaterQualityRecord.status == QUARANTINED_STATUS
    ).count()
    rejected_count = base_q.filter(WaterQualityRecord.status == REJECTED_STATUS).count()
    merge_query = db.query(WaterQualityRevision).filter(WaterQualityRevision.action == "merge")
    if batch_id:
        merge_query = merge_query.join(WaterQualityRecord).filter(
            WaterQualityRecord.batch_id == batch_id
        )
    merged_count = merge_query.count()

    return WaterQualityTrendResponse(
        batch_id=batch_id,
        points=points,
        quarantined_count=quarantined_count,
        rejected_count=rejected_count,
        merged_count=merged_count,
    )


@router.get("/trend/export.csv")
def export_water_quality_trend(batch_id: int = None, db: Session = Depends(get_db)):
    """导出与趋势图完全一致的有效数据集；缺测留空，零值写 0。"""
    records = get_valid_records(db, batch_id)
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer)
    headers = ["记录ID", "批次ID", "采样时间", "检测日期", "检测时间", "设备编号"]
    headers += [f"{spec.label}({spec.unit})" if spec.unit else spec.label for spec in METRIC_SPECS.values()]
    headers += ["状态", "备注"]
    writer.writerow(headers)
    for r in records:
        row = [
            r.id,
            r.batch_id,
            r.sampled_at.strftime("%Y-%m-%d %H:%M") if r.sampled_at else "",
            r.record_date.isoformat() if r.record_date else "",
            r.record_time or "",
            r.device_id or "manual",
        ]
        for key in METRIC_KEYS:
            value = getattr(r, key)
            row.append("" if value is None else f"{value:g}")
        row += [r.status, r.notes or ""]
        writer.writerow(row)
    content = buffer.getvalue()
    filename = f"water-quality-valid-{batch_id or 'all'}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{record_id}/", response_model=WaterQualityRecordResponse)
def get_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    return serialize_record(record)


@router.put("/{record_id}/", response_model=WaterQualityRecordResponse)
async def update_water_quality_record(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    db_record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")

    try:
        raw = await request.json()
    except Exception:
        raise _error_response(400, {"form": "请求体必须是合法的 JSON"})
    try:
        payload = WaterQualityRecordUpdate(**(raw if isinstance(raw, dict) else {}))
    except ValidationError as exc:
        raise _error_response(400, _pydantic_field_errors(exc))

    # exclude_unset：未提交的字段保持原值；显式提交 null 表示「缺测」，与 0 区分
    update_data = payload.model_dump(exclude_unset=True)

    merged_view = {
        "batch_id": update_data.get("batch_id", db_record.batch_id),
        "record_date": update_data.get("record_date", db_record.record_date),
        "record_time": update_data.get("record_time", db_record.record_time),
        "device_id": update_data.get("device_id", db_record.device_id or "manual"),
    }
    for key in METRIC_KEYS:
        merged_view[key] = update_data[key] if key in update_data else getattr(db_record, key)

    db_batch = get_batch_or_none(db, merged_view["batch_id"])
    if not db_batch:
        raise _error_response(404, {"batch_id": "批次不存在"})

    # 仅当采样日期/时间实际发生变化时才重新校验上传窗口；
    # 只更正老记录的指标值（表单原样带回日期）不受 24h 窗口限制
    window_touched = (
        ("record_date" in update_data and update_data["record_date"] != db_record.record_date)
        or ("record_time" in update_data and update_data["record_time"] != db_record.record_time)
    )
    sampled_at, hard_errors, soft_errors = validate_against_batch(
        db_batch, merged_view, enforce_upload_window=window_touched
    )
    if hard_errors:
        raise _error_response(400, {**soft_errors, **hard_errors},
                              message="存在必须修正后才能提交的字段")
    if soft_errors:
        raise _error_response(400, soft_errors)

    duplicate = find_duplicate(
        db,
        batch_id=merged_view["batch_id"],
        device_id=merged_view["device_id"],
        sampled_at=sampled_at,
        exclude_id=db_record.id,
    )
    if duplicate is not None:
        raise _error_response(
            409,
            {
                "record_date": f"同一设备 {merged_view['device_id']} 在该采样时刻已有记录"
                f"（记录 #{duplicate.id}），重复数据请走归并而非新建"
            },
            message="与已有记录的采样时间冲突",
        )

    changes = {}
    tracked_fields = ("batch_id", "record_date", "record_time", "device_id", "notes", *METRIC_KEYS)
    for key in tracked_fields:
        if key not in update_data:
            continue
        old_value = getattr(db_record, key)
        new_value = update_data[key]
        if old_value != new_value:
            changes[key] = {"old": old_value, "new": new_value}
        setattr(db_record, key, new_value)

    db_record.sampled_at = sampled_at
    previous_status = db_record.status
    # 人工更正通过校验后恢复为有效；若原记录处于隔离/作废状态，同时留下复核结论
    db_record.status = VALID_STATUS
    db_record.invalid_fields = None
    if previous_status != VALID_STATUS:
        conclusion = (
            f"经人工更正并通过校验，由 {previous_status} 恢复有效；"
            f"更正字段：{', '.join(changes.keys()) if changes else '无'}"
        )
        db_record.review_conclusion = conclusion
        db_record.reviewed_by = merged_view["device_id"]
        db_record.reviewed_at = datetime.utcnow()
    add_revision(
        db,
        db_record,
        action="update",
        changed_by=merged_view["device_id"],
        field_changes={
            **changes,
            **({"review": {"from": previous_status, "to": VALID_STATUS}}
               if previous_status != VALID_STATUS else {}),
        } if (changes or previous_status != VALID_STATUS) else {"info": "提交内容与现值一致"},
    )

    db.commit()
    db.refresh(db_record)
    return serialize_record(db_record)


@router.post("/{record_id}/review", response_model=WaterQualityRecordResponse)
def review_water_quality_record(
    record_id: int,
    review: WaterQualityReviewRequest,
    db: Session = Depends(get_db),
):
    """复核隔离记录：确认作废（rejected）或确认有效（可附带人工更正值）。"""
    record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    if record.status != QUARANTINED_STATUS:
        raise _error_response(
            409,
            {"status": f"仅隔离(quarantined)记录可复核，当前状态为 {record.status}"},
            message="记录状态不允许复核",
        )

    record.review_conclusion = review.conclusion
    record.reviewed_by = review.reviewed_by
    record.reviewed_at = datetime.utcnow()

    if review.action == "confirm_invalid":
        record.status = REJECTED_STATUS
        add_revision(
            db,
            record,
            action="review",
            changed_by=review.reviewed_by,
            field_changes={"review": "confirm_invalid", "conclusion": review.conclusion},
        )
        db.commit()
        db.refresh(record)
        return serialize_record(record)

    # confirm_valid：先应用更正值（如有），再按完整规则复验
    corrections = review.corrections or {}
    unknown = [k for k in corrections if k not in METRIC_KEYS]
    if unknown:
        raise _error_response(400, {k: "不是可更正的水质指标" for k in unknown})

    correction_errors = {}
    for key, value in corrections.items():
        reason = validate_metric(key, value)
        if reason:
            correction_errors[key] = reason
    if correction_errors:
        raise _error_response(400, correction_errors, message="更正值仍未通过指标校验")

    merged_view = {
        "batch_id": record.batch_id,
        "record_date": record.record_date,
        "record_time": record.record_time,
        "device_id": record.device_id or "manual",
    }
    for key in METRIC_KEYS:
        merged_view[key] = corrections[key] if key in corrections else getattr(record, key)

    db_batch = get_batch_or_none(db, record.batch_id)
    # 复核的是历史记录，上传窗口不再作为障碍；批次有效期与指标边界仍须满足
    _, remaining_hard, remaining_soft = validate_against_batch(
        db_batch, merged_view, enforce_upload_window=False
    )
    remaining_errors = {**remaining_hard, **remaining_soft}
    if remaining_errors:
        raise _error_response(
            400,
            remaining_errors,
            message="记录仍无法通过校验：指标问题请在 corrections 中给出更正值，"
                    "采样日期/批次有效期问题请改用「更正」直接修改记录",
        )

    changes = {}
    for key, value in corrections.items():
        old_value = getattr(record, key)
        if old_value != value:
            changes[key] = {"old": old_value, "new": value}
    apply_metrics(record, corrections)
    record.status = VALID_STATUS
    record.invalid_fields = None
    add_revision(
        db,
        record,
        action="review",
        changed_by=review.reviewed_by,
        field_changes={
            "review": "confirm_valid",
            "conclusion": review.conclusion,
            "corrections": changes,
        },
    )
    db.commit()
    db.refresh(record)
    return serialize_record(record)


@router.delete("/{record_id}/")
def delete_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    db_record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    if db_record.status in (QUARANTINED_STATUS, REJECTED_STATUS):
        raise _error_response(
            409,
            {"status": "异常记录只能隔离/复核，不得删除；请先在复核面板给出复核结论"},
            message="异常记录受删除保护",
        )

    db.delete(db_record)
    db.commit()
    return {"message": "水质监测记录删除成功"}
