"""水质记录校验规则：按指标、单位与采样时间判断有效性。

- 六项指标各有物理边界，非有限数（NaN / Infinity）一律拒绝；
- None 表示缺测，0 是合法测量值，二者严格区分；
- 采样时间必须落在合理上传窗口内，且处于养殖批次有效期内。

校验结果以「字段 -> 中文原因」的形式返回，便于前端在表单中精确定位错误字段。
"""

import math
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional, Tuple

# 允许补录的时间窗口（采样时刻距上传时刻），可用环境变量覆盖
UPLOAD_WINDOW_HOURS = float(os.getenv("WATER_QUALITY_UPLOAD_WINDOW_HOURS", "24"))
# 设备时钟误差宽限（采样时间略微晚于服务器时间时不报错）
FUTURE_GRACE = timedelta(minutes=int(os.getenv("WATER_QUALITY_FUTURE_GRACE_MINUTES", "60")))

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    unit: str
    min_value: float
    max_value: float

    def out_of_range_message(self, value: float) -> str:
        return f"{self.label}必须在 {self.min_value:g}~{self.max_value:g}{self.unit} 之间，实测 {value:g}{self.unit}"


# 各指标的有效边界（含端点），按录入/展示顺序排列
METRIC_SPECS: "OrderedDict[str, MetricSpec]" = OrderedDict(
    [
        ("water_temperature", MetricSpec("water_temperature", "水温", "℃", -2, 45)),
        ("ph_value", MetricSpec("ph_value", "酸碱度(pH)", "", 0, 14)),
        ("dissolved_oxygen", MetricSpec("dissolved_oxygen", "溶解氧", "mg/L", 0, 25)),
        ("ammonia_nitrogen", MetricSpec("ammonia_nitrogen", "氨氮", "mg/L", 0, 10)),
        ("nitrite", MetricSpec("nitrite", "亚硝酸盐", "mg/L", 0, 5)),
        ("transparency", MetricSpec("transparency", "透明度", "cm", 0, 500)),
    ]
)
METRIC_KEYS = tuple(METRIC_SPECS.keys())


class WaterQualityValidationError(Exception):
    """携带字段级错误信息的校验异常。"""

    def __init__(self, field_errors: Dict[str, str]):
        self.field_errors = dict(field_errors)
        super().__init__("；".join(f"{k}: {v}" for k, v in self.field_errors.items()))


def coerce_metric(value: Any) -> Optional[float]:
    """把入参转为 float；空串/None 视为缺测（None）。"""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return float(value)


def validate_metric(key: str, value: Any) -> Optional[str]:
    """校验单个指标，返回错误原因；None（缺测）与 0（实测零值）均合法。"""
    spec = METRIC_SPECS.get(key)
    if spec is None:
        return None
    try:
        number = coerce_metric(value)
    except (TypeError, ValueError):
        return f"{spec.label}必须是数值"
    if number is None:
        return None
    if not math.isfinite(number):
        return f"{spec.label}必须是有限数值，不能为无穷大或 NaN"
    if number < spec.min_value or number > spec.max_value:
        return spec.out_of_range_message(number)
    return None


def combine_sampled_at(record_date: date, record_time: Optional[str]) -> datetime:
    """把检测日期与 HH:MM 时间合并为采样时刻；缺时间时按当日 00:00 处理。"""
    if record_time is None or record_time.strip() == "":
        return datetime.combine(record_date, time.min)
    if not TIME_PATTERN.match(record_time.strip()):
        raise WaterQualityValidationError(
            {"record_time": "检测时间格式应为 HH:MM（24小时制），例如 08:30"}
        )
    hour, minute = (int(part) for part in record_time.strip().split(":"))
    return datetime.combine(record_date, time(hour=hour, minute=minute))


def validate_payload(
    *,
    batch,
    record_date: date,
    record_time: Optional[str],
    metric_values: Dict[str, Any],
    now: Optional[datetime] = None,
    enforce_upload_window: bool = True,
) -> Tuple[datetime, Dict[str, str], Dict[str, str]]:
    """完整校验一条水质记录。

    返回 (采样时刻, 硬性错误, 可隔离错误)，错误均为「字段 -> 中文原因」：
    - 硬性错误：非有限数、时间格式错误等，任何情况下都必须拒绝；
    - 可隔离错误：越界、超出上传窗口、批次有效期外，可先隔离再人工复核。
    """
    hard_errors: Dict[str, str] = {}
    soft_errors: Dict[str, str] = {}
    current = now or datetime.utcnow()

    if record_date is None:
        hard_errors["record_date"] = "检测日期不能为空"
        return current, hard_errors, soft_errors

    try:
        sampled_at = combine_sampled_at(record_date, record_time)
    except WaterQualityValidationError as exc:
        hard_errors.update(exc.field_errors)
        sampled_at = datetime.combine(record_date, time.min)

    # 上传窗口：不能是「未来」数据，也不能补录过久以前的数据
    if "record_time" not in hard_errors and enforce_upload_window:
        if sampled_at > current + FUTURE_GRACE:
            soft_errors["record_time"] = (
                f"采样时间 {sampled_at:%Y-%m-%d %H:%M} 晚于合理上传窗口"
                f"（服务器时间 {current:%Y-%m-%d %H:%M}），请核对检测日期与时间"
            )
        elif sampled_at < current - timedelta(hours=UPLOAD_WINDOW_HOURS):
            soft_errors["record_date"] = (
                f"采样时间 {sampled_at:%Y-%m-%d %H:%M} 已超出 {UPLOAD_WINDOW_HOURS:g} 小时"
                "上传窗口，请补走复核流程或核对日期"
            )

    # 批次有效期：采样必须发生在放苗之后、实际收获（含当日）之前
    if batch is not None:
        if record_date < batch.stocking_date:
            soft_errors["record_date"] = (
                f"检测日期 {record_date:%Y-%m-%d} 早于批次放苗日期"
                f" {batch.stocking_date:%Y-%m-%d}，不在批次有效期内"
            )
        elif batch.actual_harvest_date and record_date > batch.actual_harvest_date:
            soft_errors["record_date"] = (
                f"检测日期 {record_date:%Y-%m-%d} 晚于批次实际收获日期"
                f" {batch.actual_harvest_date:%Y-%m-%d}，不在批次有效期内"
            )

    # 指标级校验：非有限数属硬性错误，越界属可隔离错误
    for key, raw in metric_values.items():
        spec = METRIC_SPECS.get(key)
        if spec is None:
            continue
        try:
            number = coerce_metric(raw)
        except (TypeError, ValueError):
            hard_errors[key] = f"{spec.label}必须是数值"
            continue
        if number is None:
            continue
        if not math.isfinite(number):
            hard_errors[key] = f"{spec.label}必须是有限数值，不能为无穷大或 NaN"
        elif number < spec.min_value or number > spec.max_value:
            soft_errors[key] = spec.out_of_range_message(number)

    return sampled_at, hard_errors, soft_errors


def audit_metrics(metric_values: Dict[str, Any]) -> Dict[str, str]:
    """只做指标审计（供历史数据迁移隔离使用）：返回全部异常字段原因。"""
    reasons: Dict[str, str] = {}
    for key, raw in metric_values.items():
        reason = validate_metric(key, raw)
        if reason:
            reasons[key] = reason
    return reasons


def metric_display(spec: MetricSpec, value: Optional[float]) -> str:
    """导出/展示用：缺测显示为空，零值显示为 0（绝不把二者混为一谈）。"""
    if value is None:
        return ""
    return f"{value:g}"
