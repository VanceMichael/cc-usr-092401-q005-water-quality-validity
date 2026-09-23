"""水质记录统一校验规则。

所有写入路径（创建 / 修改 / 设备上报）与读取路径（接口、趋势图、导出）
共用本模块的指标边界与时间窗口规则，避免“只声明字段类型”导致的
脏数据（如 pH=90、溶氧=Infinity）落库后拉平趋势。

设计要点：
- None 表示“缺测”，与合法的 0.0（例如溶氧为 0 的真实低氧事故）严格区分；
- inf / nan 一律拒绝，不参与后续判断；
- 每个指标有独立的物理 / 业务边界；
- 采样时间既不能晚于合理上传窗口（防止设备时钟漂移 / 手工误填未来时间），
  也不能落在所属批次的有效期之外。
"""

import math
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 指标边界
# 边界按“淡水池塘养殖中物理上可能出现、业务上有意义”的区间设定。
# 下限统一为 0：0 是真实测量值（溶氧 0、透明度见底），绝不能按缺测处理。
# ---------------------------------------------------------------------------

METRIC_SPECS: Dict[str, Dict[str, Any]] = {
    "water_temperature": {
        "label": "水温",
        "unit": "℃",
        "min": 0.0,
        "max": 40.0,
    },
    "ph_value": {
        "label": "酸碱度(pH)",
        "unit": "",
        "min": 0.0,
        "max": 14.0,
    },
    "dissolved_oxygen": {
        "label": "溶解氧",
        "unit": "mg/L",
        "min": 0.0,
        "max": 25.0,
    },
    "ammonia_nitrogen": {
        "label": "氨氮",
        "unit": "mg/L",
        "min": 0.0,
        "max": 10.0,
    },
    "nitrite": {
        "label": "亚硝酸盐",
        "unit": "mg/L",
        "min": 0.0,
        "max": 5.0,
    },
    "transparency": {
        "label": "透明度",
        "unit": "cm",
        "min": 0.0,
        "max": 500.0,
    },
}

METRIC_FIELDS = tuple(METRIC_SPECS.keys())

# 采样时间允许“晚于当前时刻”的最大宽限（设备时钟漂移、补录缓冲）。
UPLOAD_FUTURE_TOLERANCE_HOURS = float(
    os.getenv("WQ_UPLOAD_FUTURE_TOLERANCE_HOURS", "24")
)
# 允许补录的最早时间（防止把年份误录成 1900 / 0001 之类）。
EARLIEST_SAMPLE_DATE = date(1990, 1, 1)
# 采样日期落在批次放苗 / 收获日之外的宽限天数。
BATCH_BOUNDARY_GRACE_DAYS = 1


class FieldError(Exception):
    """字段级校验错误集合，可直接转换为 422 响应。"""

    def __init__(self, errors: List[Dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


def make_error(field: str, message: str) -> Dict[str, str]:
    return {"field": field, "message": message}


def is_finite_number(value: Any) -> bool:
    """0 和 0.0 是有限数；bool 不算数值；inf/nan 不是。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def validate_metric(field: str, value: Any) -> Optional[Dict[str, str]]:
    """校验单个指标。

    返回 None 表示通过（含 None 缺测）；否则返回 {field, message}。
    """
    if value is None:
        return None  # 缺测合法，由上层语义决定
    spec = METRIC_SPECS[field]
    label, unit = spec["label"], spec["unit"]
    unit_text = f" {unit}" if unit else ""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return make_error(field, f"{label}必须是数值，收到的是“{value}”")
    numeric = float(value)
    if not math.isfinite(numeric):
        # 化验员把无穷大 / NaN 作为溶氧值的情况必须在此挡住
        return make_error(field, f"{label}必须是有限数值，不能是无穷大或非数字")
    if numeric < spec["min"] or numeric > spec["max"]:
        return make_error(
            field,
            f"{label}超出有效范围"
            f"（{spec['min']:g}~{spec['max']:g}{unit_text}），实测 {numeric:g}",
        )
    return None


def validate_record_time(record_time: Optional[str]) -> Optional[Dict[str, str]]:
    """校验 HH:MM（24 小时制）格式；空串视为缺测。"""
    if record_time is None or record_time == "":
        return None
    if not isinstance(record_time, str):
        return make_error("record_time", "检测时间格式应为 HH:MM")
    text = record_time.strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return make_error("record_time", "检测时间格式不正确，应为 HH:MM（如 08:30）")
    if parsed.strftime("%H:%M") != text:
        return make_error("record_time", "检测时间格式不正确，应为 HH:MM（如 08:30）")
    return None


def combine_sample_datetime(
    record_date: date, record_time: Optional[str]
) -> datetime:
    """把日期 + 时间字符串合并为采样时刻；时间缺测按当日 00:00 处理。"""
    if record_time:
        try:
            parsed = datetime.strptime(record_time.strip(), "%H:%M")
            return datetime.combine(record_date, parsed.time())
        except ValueError:
            pass
    return datetime.combine(record_date, datetime.min.time())


def validate_sample_window(
    sample_dt: datetime,
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, str]]:
    """校验采样时间是否落在“合理上传窗口”内（与批次无关的部分）。"""
    current = now or datetime.utcnow()
    if sample_dt.date() < EARLIEST_SAMPLE_DATE:
        return make_error(
            "record_date",
            f"采样日期 {sample_dt.date().isoformat()} 过早，系统不接受 "
            f"{EARLIEST_SAMPLE_DATE.isoformat()} 之前的记录",
        )
    latest = current + timedelta(hours=UPLOAD_FUTURE_TOLERANCE_HOURS)
    if sample_dt > latest:
        return make_error(
            "record_date",
            f"采样时间 {sample_dt.strftime('%Y-%m-%d %H:%M')} 晚于合理上传窗口"
            f"（当前时间 +{UPLOAD_FUTURE_TOLERANCE_HOURS:g} 小时，"
            f"即不得晚于 {latest.strftime('%Y-%m-%d %H:%M')}），"
            "请核对设备时钟或录入时间",
        )
    return None


def validate_batch_period(
    sample_dt: datetime,
    batch_stocking_date: date,
    batch_actual_harvest_date: Optional[date],
) -> Optional[Dict[str, str]]:
    """校验采样时间是否落在批次有效期（放苗 ~ 收获，含宽限）内。"""
    grace = timedelta(days=BATCH_BOUNDARY_GRACE_DAYS)
    earliest = datetime.combine(batch_stocking_date, datetime.min.time()) - grace
    if sample_dt < earliest:
        return make_error(
            "record_date",
            f"采样时间 {sample_dt.date().isoformat()} 早于批次放苗日期 "
            f"{batch_stocking_date.isoformat()}（宽限 {BATCH_BOUNDARY_GRACE_DAYS} 天），"
            "不能计入该批次",
        )
    if batch_actual_harvest_date is not None:
        latest = datetime.combine(
            batch_actual_harvest_date, datetime.max.time()
        ) + grace
        if sample_dt > latest:
            return make_error(
                "record_date",
                f"采样时间 {sample_dt.date().isoformat()} 晚于批次收获日期 "
                f"{batch_actual_harvest_date.isoformat()}（宽限 {BATCH_BOUNDARY_GRACE_DAYS} 天），"
                "该批次已结束，不能继续计入",
            )
    return None


def validate_water_quality_payload(
    payload: Dict[str, Any],
    *,
    batch_stocking_date: Optional[date] = None,
    batch_actual_harvest_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Optional[float]], List[Dict[str, str]], Dict[str, str]]:
    """对一条水质记录的完整字段做校验。

    返回 (clean_metrics, errors, raw_strings)：
    - clean_metrics: 通过校验的指标值（None 表示缺测）；
    - errors: 字段级错误列表，空列表表示全部通过；
    - raw_strings: 指标的原始文本（用于隔离留痕，记录化验员实际录入内容）。
    """
    errors: List[Dict[str, str]] = []
    clean: Dict[str, Optional[float]] = {}
    raw: Dict[str, str] = {}

    record_date = payload.get("record_date")
    if not isinstance(record_date, date) or isinstance(record_date, datetime):
        if not isinstance(record_date, date):
            errors.append(make_error("record_date", "检测日期必填且应为有效日期"))
            record_date = None
    record_time = payload.get("record_time")
    time_error = validate_record_time(record_time)
    if time_error:
        errors.append(time_error)

    if record_date is not None:
        sample_dt = combine_sample_datetime(record_date, record_time)
        window_error = validate_sample_window(sample_dt, now=now)
        if window_error:
            errors.append(window_error)
        if batch_stocking_date is not None:
            period_error = validate_batch_period(
                sample_dt, batch_stocking_date, batch_actual_harvest_date
            )
            if period_error:
                errors.append(period_error)

    for field in METRIC_FIELDS:
        value = payload.get(field)
        raw[field] = "" if value is None else str(value)
        if value is None:
            clean[field] = None  # 缺测
            continue
        err = validate_metric(field, value)
        if err:
            errors.append(err)
            clean[field] = None  # 该字段隔离，不参与有效数据集
        else:
            clean[field] = float(value)

    return clean, errors, raw


def metric_quality_flags(record: Any) -> Dict[str, str]:
    """根据记录的隔离字段清单，给出每个指标的有效状态。

    - "valid"：有效测量值（含合法的 0）；
    - "missing"：缺测（录入时留空）；
    - "quarantined"：有值但未通过校验，被隔离待复核。
    """
    quarantined = set(getattr(record, "quarantined_fields_list", None) or [])
    flags: Dict[str, str] = {}
    for field in METRIC_FIELDS:
        value = getattr(record, field, None)
        if field in quarantined:
            flags[field] = "quarantined"
        elif value is None:
            flags[field] = "missing"
        else:
            flags[field] = "valid"
    return flags
