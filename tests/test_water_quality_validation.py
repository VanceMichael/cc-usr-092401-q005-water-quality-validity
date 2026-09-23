"""水质记录校验、隔离复核、幂等归并与有效数据集一致性测试。"""

import asyncio
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from typing import Any, Dict

# 必须在导入 backend.app.* 之前指定测试数据库
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "aqua_unittest.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ["WATER_QUALITY_UPLOAD_WINDOW_HOURS"] = "24"

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.app.database import Base, SessionLocal, engine  # noqa: E402
from backend.app.main import run_migrations  # noqa: E402
from backend.app.models import Batch, Pond, WaterQualityRecord  # noqa: E402
from backend.app.routers import water_quality as wq_router  # noqa: E402
from backend.app.routers import analysis as analysis_router  # noqa: E402
from backend.app.schemas import WaterQualityReviewRequest  # noqa: E402
from backend.app.validation import (  # noqa: E402
    METRIC_KEYS,
    validate_metric,
    validate_payload,
    WaterQualityValidationError,
)
from backend.app import migrations as migrations_mod  # noqa: E402


class FakeRequest:
    """模拟 FastAPI Request，仅提供路由用到的 json()。"""

    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload

    async def json(self):
        return self._payload


def acall(coro):
    return asyncio.run(coro)


class WaterQualityTestCase(unittest.TestCase):
    def setUp(self):
        # 不能在引擎已建连后删除数据库文件（连接池会持有失效 inode），
        # 直接 drop/create 重建全表即可
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        run_migrations()
        self.db = SessionLocal()
        self.today = date.today()
        self.pond = Pond(name="测试塘", area=10, water_depth=2, species="鲈鱼")
        self.db.add(self.pond)
        self.db.flush()
        self.batch = Batch(
            batch_number="T-001",
            pond_id=self.pond.id,
            species="鲈鱼",
            stocking_date=self.today - timedelta(days=100),
            estimated_harvest_date=self.today + timedelta(days=100),
            status="active",
        )
        self.db.add(self.batch)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    # ---- 辅助 ----
    def create(self, payload: Dict[str, Any], accept_quarantine: bool = False):
        return acall(wq_router.create_water_quality_record(
            FakeRequest(payload), accept_quarantine, self.db
        ))

    def create_expect_error(self, payload: Dict[str, Any], accept_quarantine: bool = False):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.create(payload, accept_quarantine)
        return ctx.exception

    def update(self, record_id: int, payload: Dict[str, Any]):
        return acall(wq_router.update_water_quality_record(
            record_id, FakeRequest(payload), self.db
        ))

    def valid_payload(self, **overrides):
        d, t = self.at_offset(-1)
        payload = {
            "batch_id": self.batch.id,
            "record_date": d,
            "record_time": t,
            "device_id": "sensor-1",
        }
        payload.update(overrides)
        return payload

    def at_offset(self, hours_offset: float):
        """相对当前 UTC 的采样时刻（日期, HH:MM），保证落在上传窗口内。"""
        moment = datetime.utcnow() + timedelta(hours=hours_offset)
        return moment.date().isoformat(), moment.strftime("%H:%M")

    def field_errors(self, exc):
        return exc.detail["field_errors"]

    # ---- 指标边界 ----
    def test_metric_bounds_unit_level(self):
        self.assertIsNone(validate_metric("ph_value", 0))
        self.assertIsNone(validate_metric("ph_value", 14))
        self.assertIsNone(validate_metric("dissolved_oxygen", 0))
        self.assertIsNone(validate_metric("water_temperature", -2))
        self.assertIsNone(validate_metric("water_temperature", 45))
        self.assertIn("酸碱度", validate_metric("ph_value", 90))
        self.assertIn("溶解氧", validate_metric("dissolved_oxygen", 30))
        self.assertIn("透明度", validate_metric("transparency", -1))
        self.assertIn("有限", validate_metric("ph_value", float("inf")))
        self.assertIn("有限", validate_metric("ammonia_nitrogen", float("nan")))

    def test_ph_90_and_out_of_range_rejected_with_field_location(self):
        exc = self.create_expect_error(self.valid_payload(ph_value=90, dissolved_oxygen=30))
        errors = self.field_errors(exc)
        self.assertIn("ph_value", errors)
        self.assertIn("dissolved_oxygen", errors)

    def test_all_six_metrics_have_independent_bounds(self):
        cases = {
            "water_temperature": 46,
            "ph_value": 14.5,
            "dissolved_oxygen": 26,
            "ammonia_nitrogen": 11,
            "nitrite": 6,
            "transparency": 501,
        }
        for key, bad_value in cases.items():
            exc = self.create_expect_error(self.valid_payload(**{key: bad_value}))
            self.assertIn(key, self.field_errors(exc), f"{key} 越界应被独立定位")

    # ---- 非有限数 ----
    def test_infinity_and_nan_always_rejected_even_with_quarantine_flag(self):
        for token in (float("inf"), float("-inf"), float("nan")):
            exc = self.create_expect_error(
                self.valid_payload(dissolved_oxygen=token), accept_quarantine=True
            )
            self.assertIn("dissolved_oxygen", self.field_errors(exc))
            self.assertEqual(self.db.query(WaterQualityRecord).count(), 0)

    # ---- 缺测与零值 ----
    def test_missing_none_distinct_from_zero(self):
        record = self.create(self.valid_payload(dissolved_oxygen=0))
        self.assertEqual(record["status"], "valid")
        self.assertEqual(record["dissolved_oxygen"], 0.0)
        self.assertIsNone(record["ph_value"])

        db_record = self.db.query(WaterQualityRecord).filter_by(id=record["id"]).one()
        self.assertEqual(db_record.dissolved_oxygen, 0.0)
        self.assertIsNone(db_record.ph_value)

        trend = wq_router.get_water_quality_trend(self.batch.id, None, None, self.db)
        point = trend.points[0]
        self.assertEqual(point.dissolved_oxygen, 0.0)
        self.assertIsNone(point.ph_value)

        csv_body = wq_router.export_water_quality_trend(self.batch.id, self.db).body.decode("utf-8")
        data_row = [line for line in csv_body.splitlines() if line.startswith(f"{record['id']},")][0]
        # 表头: ...设备,水温,pH,溶氧,... 缺测为空，零值写成 0
        self.assertIn(",0,", data_row)

    # ---- 采样时间窗口 ----
    def test_future_sample_time_rejected(self):
        future_date = (self.today + timedelta(days=3)).isoformat()
        exc = self.create_expect_error(self.valid_payload(record_date=future_date))
        self.assertIn("record_time", self.field_errors(exc))

    def test_stale_sample_beyond_upload_window_rejected(self):
        old_date = (self.today - timedelta(hours=25)).isoformat()
        exc = self.create_expect_error(self.valid_payload(record_date=old_date))
        self.assertIn("record_date", self.field_errors(exc))

    def test_bad_time_format_hard_error(self):
        exc = self.create_expect_error(self.valid_payload(record_time="8点半"))
        self.assertIn("record_time", self.field_errors(exc))

    # ---- 批次有效期 ----
    def test_sample_outside_batch_window_rejected(self):
        exc = self.create_expect_error(self.valid_payload(
            record_date=(self.today - timedelta(days=120)).isoformat()))
        self.assertIn("record_date", self.field_errors(exc))

        self.batch.actual_harvest_date = self.today - timedelta(days=2)
        self.db.commit()
        exc = self.create_expect_error(self.valid_payload(
            record_date=(self.today - timedelta(days=1)).isoformat()))
        self.assertIn("record_date", self.field_errors(exc))

    def test_soft_violation_can_be_quarantined(self):
        record = self.create(
            self.valid_payload(record_date=(self.today - timedelta(days=120)).isoformat()),
            accept_quarantine=True,
        )
        self.assertEqual(record["status"], "quarantined")
        self.assertIn("record_date", record["invalid_field_reasons"])

    # ---- 幂等归并 ----
    def test_duplicate_same_device_same_sample_is_idempotent_merge(self):
        first = self.create(self.valid_payload(dissolved_oxygen=5.1))
        second = self.create(self.valid_payload(dissolved_oxygen=5.1, ph_value=7.2))
        self.assertTrue(second["merged_from_duplicate"])
        self.assertEqual(first["id"], second["id"])

        valid = wq_router.get_water_quality_records(
            skip=0, limit=1000, batch_id=self.batch.id, status="valid",
            include_merged=False, db=self.db,
        )
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["ph_value"], 7.2)
        actions = [r["action"] for r in valid[0]["revisions"]]
        self.assertIn("create", actions)
        self.assertIn("merge", actions)
        trend = wq_router.get_water_quality_trend(self.batch.id, None, None, self.db)
        self.assertEqual(len(trend.points), 1)
        self.assertGreaterEqual(trend.merged_count, 1)

    def test_different_devices_same_sample_are_separate_records(self):
        first = self.create(self.valid_payload(device_id="sensor-A", dissolved_oxygen=5))
        second = self.create(self.valid_payload(device_id="sensor-B", dissolved_oxygen=5))
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second["merged_from_duplicate"])

    def test_update_into_other_sample_time_conflict_rejected(self):
        d1, t1 = self.at_offset(-1)
        d2, t2 = self.at_offset(-2)
        self.create(self.valid_payload(device_id="sensor-1", record_date=d1, record_time=t1, ph_value=7))
        other = self.create(self.valid_payload(device_id="sensor-1", record_date=d2, record_time=t2, ph_value=8))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.update(other["id"], {"record_date": d1, "record_time": t1})
        self.assertEqual(ctx.exception.status_code, 409)

    # ---- 隔离复核 ----
    def test_quarantine_reject_then_protected_and_excluded(self):
        bad = self.create(self.valid_payload(ph_value=90), accept_quarantine=True)
        self.assertEqual(bad["status"], "quarantined")

        reviewed = wq_router.review_water_quality_record(
            bad["id"],
            WaterQualityReviewRequest(
                action="confirm_invalid",
                conclusion="化验员误录 pH，复测为 7.2，原值作废",
                reviewed_by="张三",
            ),
            self.db,
        )
        self.assertEqual(reviewed["status"], "rejected")
        self.assertIn("作废", reviewed["review_conclusion"])

        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            wq_router.delete_water_quality_record(bad["id"], self.db)
        self.assertEqual(ctx.exception.status_code, 409)

        # 记录仍可回看，但不进入任何有效数据集
        fetched = wq_router.get_water_quality_record(bad["id"], self.db)
        self.assertEqual(fetched["status"], "rejected")
        trend = wq_router.get_water_quality_trend(self.batch.id, None, None, self.db)
        self.assertEqual(len(trend.points), 0)
        self.assertEqual(trend.rejected_count, 1)

        trace = analysis_router.batch_traceability(self.batch.id, self.db)
        self.assertEqual(len(trace.water_quality_records), 0)

        csv_body = wq_router.export_water_quality_trend(self.batch.id, self.db).body.decode("utf-8")
        self.assertEqual(len(csv_body.strip().splitlines()), 1)  # 仅表头

    def test_quarantine_confirm_valid_with_correction(self):
        bad = self.create(self.valid_payload(ph_value=90, dissolved_oxygen=3.1),
                          accept_quarantine=True)
        fixed = wq_router.review_water_quality_record(
            bad["id"],
            WaterQualityReviewRequest(
                action="confirm_valid",
                conclusion="复测 pH 为 7.4",
                reviewed_by="李四",
                corrections={"ph_value": 7.4},
            ),
            self.db,
        )
        self.assertEqual(fixed["status"], "valid")
        self.assertEqual(fixed["ph_value"], 7.4)
        trend = wq_router.get_water_quality_trend(self.batch.id, None, None, self.db)
        self.assertEqual(len(trend.points), 1)

    def test_review_correction_still_invalid_is_rejected(self):
        bad = self.create(self.valid_payload(ph_value=90), accept_quarantine=True)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            wq_router.review_water_quality_record(
                bad["id"],
                WaterQualityReviewRequest(
                    action="confirm_valid",
                    conclusion="再错一次",
                    corrections={"ph_value": 91},
                ),
                self.db,
            )
        self.assertIn("ph_value", ctx.exception.detail["field_errors"])

    # ---- 修订留痕 ----
    def test_sensor_original_and_manual_correction_both_retained(self):
        created = self.create(self.valid_payload(
            device_id="sensor-9", dissolved_oxygen=2.0, ph_value=7.0))
        updated = self.update(created["id"], {"dissolved_oxygen": 6.8})
        actions = [(r["action"], r["changed_by"]) for r in updated["revisions"]]
        self.assertIn(("create", "sensor-9"), actions)
        self.assertTrue(any(a == "update" for a, _ in actions))
        update_rev = [r for r in updated["revisions"] if r["action"] == "update"][0]
        self.assertEqual(update_rev["field_changes"]["dissolved_oxygen"]["old"], 2.0)
        self.assertEqual(update_rev["field_changes"]["dissolved_oxygen"]["new"], 6.8)

    def test_correcting_old_record_does_not_trigger_upload_window(self):
        # 超出 24h 上传窗口的补录先隔离；复核恢复有效后，再更正指标不应被窗口拦截
        old = self.create(
            self.valid_payload(record_date=(self.today - timedelta(days=10)).isoformat(),
                               ph_value=7.0),
            accept_quarantine=True,
        )
        self.assertEqual(old["status"], "quarantined")
        fixed = wq_router.review_water_quality_record(
            old["id"],
            WaterQualityReviewRequest(
                action="confirm_valid", conclusion="历史补录复核，数据有效"
            ),
            self.db,
        )
        self.assertEqual(fixed["status"], "valid")
        again = self.update(fixed["id"], {"nitrite": 0.05})
        self.assertEqual(again["nitrite"], 0.05)
        self.assertEqual(again["status"], "valid")

    def test_edit_form_resubmitting_old_date_is_not_blocked_by_window(self):
        # 前端编辑表单会原样带回旧采样日期/时间；只改指标不得被上传窗口拦截
        old_d, old_t = self.at_offset(-72)
        created = self.create(self.valid_payload(
            record_date=old_d, record_time=old_t, ph_value=7.0),
            accept_quarantine=True)
        self.assertEqual(created["status"], "quarantined")
        fixed = wq_router.review_water_quality_record(
            created["id"],
            WaterQualityReviewRequest(action="confirm_valid", conclusion="补录复核通过"),
            self.db,
        )
        updated = self.update(fixed["id"], {
            "batch_id": self.batch.id,
            "record_date": old_d,
            "record_time": old_t,
            "device_id": "sensor-1",
            "ph_value": 7.3,
        })
        self.assertEqual(updated["status"], "valid")
        self.assertEqual(updated["ph_value"], 7.3)

    def test_direct_edit_of_quarantined_record_restores_validity_with_audit(self):
        bad = self.create(self.valid_payload(record_time=None, ph_value=90),
                          accept_quarantine=True)
        self.assertEqual(bad["status"], "quarantined")
        restored = self.update(bad["id"], {"ph_value": 7.5})
        self.assertEqual(restored["status"], "valid")
        self.assertEqual(restored["ph_value"], 7.5)
        self.assertIsNotNone(restored["review_conclusion"])
        self.assertIn("quarantined", restored["review_conclusion"])
        actions = [r["action"] for r in restored["revisions"]]
        self.assertIn("update", actions)

    # ---- 接口/趋势/导出/分析一致性 ----
    def test_trend_export_traceability_share_one_valid_dataset(self):
        self.create(self.valid_payload(record_date=self.at_offset(-6)[0], record_time=self.at_offset(-6)[1], dissolved_oxygen=6.0))
        self.create(self.valid_payload(record_date=self.at_offset(-4)[0], record_time=self.at_offset(-4)[1], dissolved_oxygen=5.5))
        self.create(self.valid_payload(record_date=self.at_offset(-2)[0], record_time=self.at_offset(-2)[1], dissolved_oxygen=3.2))
        dq, tq = self.at_offset(-1)
        quarantined = self.create(
            self.valid_payload(record_date=dq, record_time=tq, ph_value=90),
            accept_quarantine=True,
        )
        wq_router.review_water_quality_record(
            quarantined["id"],
            WaterQualityReviewRequest(action="confirm_invalid", conclusion="作废"),
            self.db,
        )

        trend = wq_router.get_water_quality_trend(self.batch.id, None, None, self.db)
        valid_ids = sorted(p.record_id for p in trend.points)
        self.assertEqual(len(valid_ids), 3)

        csv_body = wq_router.export_water_quality_trend(self.batch.id, self.db).body.decode("utf-8")
        reader = list(csv.DictReader(io.StringIO(csv_body.lstrip("﻿"))))
        csv_ids = sorted(int(row["记录ID"]) for row in reader)
        self.assertEqual(csv_ids, valid_ids)
        for row in reader:
            self.assertEqual(row["状态"], "valid")

        trace = analysis_router.batch_traceability(self.batch.id, self.db)
        # trace schema 只回部分字段且无 id，按溶氧值集合与有效数据集核对
        trace_do = sorted(r.dissolved_oxygen for r in trace.water_quality_records)
        self.assertEqual(trace_do, [3.2, 5.5, 6.0])
        self.assertEqual(len(trace.water_quality_records), 3)

    def test_batch_not_found_field_error(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.create(self.valid_payload(batch_id=99999))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("batch_id", ctx.exception.detail["field_errors"])

    # ---- 历史数据迁移：旧异常值隔离而非删除 ----
    def test_legacy_bad_values_are_quarantined_on_migration(self):
        legacy_path = os.path.join(tempfile.gettempdir(), "aqua_unittest_legacy.db")
        if os.path.exists(legacy_path):
            os.remove(legacy_path)
        legacy_engine = create_engine(f"sqlite:///{legacy_path}")
        with legacy_engine.begin() as conn:
            # 模拟升级前的旧表结构
            conn.execute(text("""
                CREATE TABLE water_quality_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    record_date DATE NOT NULL,
                    record_time VARCHAR(20),
                    water_temperature FLOAT,
                    ph_value FLOAT,
                    dissolved_oxygen FLOAT,
                    ammonia_nitrogen FLOAT,
                    nitrite FLOAT,
                    transparency FLOAT,
                    notes TEXT,
                    created_at DATETIME
                )
            """))
            conn.execute(text("""
                INSERT INTO water_quality_records
                    (batch_id, record_date, record_time, water_temperature, ph_value,
                     dissolved_oxygen, ammonia_nitrogen, nitrite, transparency, created_at)
                VALUES
                    (1, :d, '08:00', 25, 90, 5000000000, NULL, NULL, 30, :now),
                    (1, :d, '18:00', 26, 7.2, 6.1, 0.2, 0.01, 25, :now)
            """), {"d": self.today.isoformat(), "now": datetime.utcnow().isoformat()})
        # 修订表等新表照常建立；旧水质表保留结构由迁移补列
        Base.metadata.create_all(bind=legacy_engine)

        original_engine = migrations_mod.engine
        original_session = migrations_mod.SessionLocal
        try:
            migrations_mod.engine = legacy_engine
            migrations_mod.SessionLocal = sessionmaker(bind=legacy_engine)
            migrations_mod.run_migrations()
            rows = migrations_mod.SessionLocal().query(WaterQualityRecord).order_by(
                WaterQualityRecord.id).all()
            self.assertEqual(rows[0].status, "quarantined")
            self.assertIn("ph_value", json.loads(rows[0].invalid_fields))
            self.assertIn("系统升级时自动隔离", rows[0].review_conclusion)
            self.assertEqual(rows[1].status, "valid")
            self.assertEqual(rows[1].nitrite, 0.01)  # 零值/小值不受影响
            self.assertIsNone(rows[0].ammonia_nitrogen)  # 异常行的缺测字段仍保持为空
        finally:
            migrations_mod.engine = original_engine
            migrations_mod.SessionLocal = original_session
            legacy_engine.dispose()
            os.remove(legacy_path)


if __name__ == "__main__":
    unittest.main()
