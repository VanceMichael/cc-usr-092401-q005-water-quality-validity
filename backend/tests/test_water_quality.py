"""水质记录校验、隔离、幂等归并、复核、导出与有效数据集一致性测试。"""

import csv
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

# 使用独立临时库，须在导入应用前设置
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Batch, Pond, WaterQualityRecord  # noqa: E402
from app.migrate import run_lightweight_migrations  # noqa: E402

client = TestClient(app)


def _make_batch(harvest: date | None = None) -> int:
    db = SessionLocal()
    try:
        pond = Pond(name=f"塘口-{os.getpid()}-{id(db)}", area=10, water_depth=2, species="草鱼")
        db.add(pond)
        db.flush()
        batch = Batch(
            batch_number=f"B{os.getpid()}{pond.id}{date.today().strftime('%Y%m%d')}",
            pond_id=pond.id,
            species="草鱼",
            stocking_date=date.today() - timedelta(days=60),
            estimated_harvest_date=date.today() + timedelta(days=60),
            actual_harvest_date=harvest,
            status="active",
        )
        db.add(batch)
        db.commit()
        return batch.id
    finally:
        db.close()


VALID_DAY = (date.today() - timedelta(days=1)).isoformat()


class WaterQualityValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch_id = _make_batch()
        cls.closed_batch_id = _make_batch(date.today() - timedelta(days=10))

    # ---- 入口校验：非有限数与越界 ----
    def test_manual_ph_90_is_rejected_with_field_error(self):
        resp = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY, "ph_value": 90,
        })
        self.assertEqual(resp.status_code, 422)
        errors = resp.json()["errors"]
        fields = {e["field"] for e in errors}
        self.assertIn("ph_value", fields)
        # 拒收即未落库
        db = SessionLocal()
        try:
            self.assertEqual(db.query(WaterQualityRecord).filter(
                WaterQualityRecord.batch_id == self.batch_id,
                WaterQualityRecord.ph_value == 90,
            ).count(), 0)
        finally:
            db.close()

    def test_infinity_dissolved_oxygen_is_rejected(self):
        resp = client.post("/api/water-quality-records/", content=(
            '{"batch_id": %d, "record_date": "%s", "dissolved_oxygen": Infinity}'
            % (self.batch_id, VALID_DAY)
        ), headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 422, resp.text)
        fields = {e["field"] for e in resp.json()["errors"]}
        self.assertIn("dissolved_oxygen", fields)

    def test_text_metric_is_rejected(self):
        resp = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "dissolved_oxygen": "无穷大",
        })
        self.assertEqual(resp.status_code, 422)

    def test_each_metric_has_its_own_boundary(self):
        cases = {
            "water_temperature": 45.0,
            "ph_value": 14.5,
            "dissolved_oxygen": 30.0,
            "ammonia_nitrogen": 12.0,
            "nitrite": 6.0,
            "transparency": 600.0,
        }
        for field, value in cases.items():
            resp = client.post("/api/water-quality-records/", json={
                "batch_id": self.batch_id, "record_date": VALID_DAY, field: value,
            })
            self.assertEqual(resp.status_code, 422, field)
            self.assertIn(field, {e["field"] for e in resp.json()["errors"]}, field)

    # ---- 零值与缺测 ----
    def test_zero_is_a_real_measurement_distinct_from_missing(self):
        resp = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "06:00", "dissolved_oxygen": 0,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        rid = resp.json()["id"]
        self.assertEqual(resp.json()["dissolved_oxygen"], 0)
        self.assertEqual(resp.json()["status"], "valid")

        got = client.get(f"/api/water-quality-records/{rid}/").json()
        self.assertIsNone(got["ph_value"])  # 缺测
        self.assertEqual(got["dissolved_oxygen"], 0)  # 真实零值

        export = client.get("/api/water-quality-records/export/").text
        # 缺测列导出为空（同一行 ph 列为空），真实 0 值导出为 0/0.0
        rows = list(csv.reader(ln.lstrip("﻿") for ln in export.splitlines()))
        header, *data_rows = rows
        row = [r for r in data_rows if int(r[0]) == self.batch_id and r[2] == "06:00"][-1]
        self.assertEqual(row[4], "")          # pH 缺测 → 空
        self.assertIn(float(row[5]), (0.0,))  # 溶氧真实 0 值 → 导出数字而非空

    # ---- 时间窗口与批次有效期 ----
    def test_future_sample_beyond_upload_window_rejected(self):
        future = (date.today() + timedelta(days=3)).isoformat()
        resp = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": future, "ph_value": 8,
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("record_date", {e["field"] for e in resp.json()["errors"]})

    def test_sample_outside_closed_batch_period_rejected(self):
        resp = client.post("/api/water-quality-records/", json={
            "batch_id": self.closed_batch_id,
            "record_date": (date.today() - timedelta(days=2)).isoformat(),
            "ph_value": 8,
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("record_date", {e["field"] for e in resp.json()["errors"]})

    def test_bad_time_format_rejected(self):
        resp = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "25:99", "ph_value": 8,
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("record_time", {e["field"] for e in resp.json()["errors"]})

    # ---- 传感器：异常隔离留痕 + 幂等归并 ----
    def test_sensor_bad_value_quarantined_but_preserved(self):
        payload = {
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "08:00", "device_id": "sensor-A",
            "ph_value": 90, "dissolved_oxygen": 4.2,
        }
        r1 = client.post("/api/water-quality-records/", json=payload)
        self.assertEqual(r1.status_code, 200, r1.text)
        body = r1.json()
        self.assertEqual(body["status"], "quarantined")
        self.assertEqual(body["quarantined_fields"], ["ph_value"])
        self.assertIsNone(body["ph_value"])  # 隔离字段不进有效数据
        self.assertEqual(body["original_payload"]["ph_value"], "90.0")  # 原值留痕
        self.assertEqual(body["source"], "sensor")

        # 同设备同采样重复上报 → 幂等返回同一条，不新增
        r2 = client.post("/api/water-quality-records/", json=payload)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["id"], body["id"])

        db = SessionLocal()
        try:
            count = db.query(WaterQualityRecord).filter(
                WaterQualityRecord.device_id == "sensor-A",
                WaterQualityRecord.sample_time == "08:00",
                WaterQualityRecord.merged_into_id.is_(None),
            ).count()
            self.assertEqual(count, 1)
        finally:
            db.close()

    def test_conflicting_duplicate_is_merged_with_audit(self):
        base = {
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "09:00", "device_id": "sensor-B", "ph_value": 8.1,
        }
        first = client.post("/api/water-quality-records/", json=base).json()
        second = client.post("/api/water-quality-records/", json={**base, "ph_value": 8.3})
        self.assertEqual(second.json()["id"], first["id"])
        # 以首报值为准，冲突报文留痕
        detail = client.get(f"/api/water-quality-records/{first['id']}/").json()
        self.assertEqual(detail["ph_value"], 8.1)
        self.assertTrue(any(c["reason"] == "merged" for c in detail["corrections"]))

    # ---- 复核流程 ----
    def test_review_corrected_restores_validity_and_keeps_history(self):
        created = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "14:00", "device_id": "sensor-R",
            "ph_value": 90, "dissolved_oxygen": 4.2,
        }).json()
        qid = created["id"]
        self.assertEqual(created["status"], "quarantined")
        resp = client.post(f"/api/water-quality-records/{qid}/review/", json={
            "review_status": "corrected",
            "reviewed_by": "张工",
            "review_note": "复测 pH 8.2，化验员误录 90",
            "corrections": {"ph_value": 8.2},
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "valid")
        self.assertEqual(body["ph_value"], 8.2)
        self.assertEqual(body["review_status"], "corrected")
        self.assertEqual(body["original_payload"]["ph_value"], "90.0")  # 原值仍可回看
        self.assertTrue(any(c["field_name"] == "ph_value" for c in body["corrections"]))

        # 复核 confirmed 的隔离记录不能静默删除
        other = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "15:00", "device_id": "sensor-C2", "ph_value": 99,
        }).json()
        client.post(f"/api/water-quality-records/{other['id']}/review/", json={
            "review_status": "confirmed", "reviewed_by": "张工", "review_note": "探头故障读数属实",
        })
        self.assertEqual(
            client.delete(f"/api/water-quality-records/{other['id']}/").status_code, 409
        )
        detail = client.get(f"/api/water-quality-records/{other['id']}/").json()
        self.assertEqual(detail["status"], "quarantined")

    def test_review_corrected_with_out_of_range_value_rejected(self):
        created = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "16:00", "device_id": "sensor-D2", "ph_value": 77,
        }).json()
        resp = client.post(f"/api/water-quality-records/{created['id']}/review/", json={
            "review_status": "corrected", "review_note": "x",
            "corrections": {"ph_value": 88},
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("ph_value", {e["field"] for e in resp.json()["errors"]})

    # ---- 修改 ----
    def test_update_validates_and_records_correction_history(self):
        rec = client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "12:00", "dissolved_oxygen": 6.5,
        }).json()
        bad = client.put(f"/api/water-quality-records/{rec['id']}/", json={
            "dissolved_oxygen": 999,
        })
        self.assertEqual(bad.status_code, 422)
        self.assertIn("dissolved_oxygen", {e["field"] for e in bad.json()["errors"]})

        ok = client.put(f"/api/water-quality-records/{rec['id']}/", json={
            "dissolved_oxygen": 2.0,
        })
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["dissolved_oxygen"], 2.0)
        self.assertTrue(any(
            c["field_name"] == "dissolved_oxygen" and c["old_value"] == "6.5" and c["new_value"] == "2.0"
            for c in ok.json()["corrections"]
        ))

    # ---- 有效数据集一致性：接口 / 趋势 / 导出 / 追溯 ----
    def test_analysis_traceability_and_export_use_valid_dataset_only(self):
        # 真实低氧（0 值）
        client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "12:30", "dissolved_oxygen": 0,
        })
        # 隔离记录
        client.post("/api/water-quality-records/", json={
            "batch_id": self.batch_id, "record_date": VALID_DAY,
            "record_time": "13:00", "device_id": "sensor-E", "ph_value": 90,
        })
        trace = client.get(f"/api/analysis/traceability/{self.batch_id}/").json()
        wq = trace["water_quality_records"]
        self.assertTrue(all(r.get("ph_value") != 90 for r in wq))
        # 真实低氧（0 值）必须出现在追溯中，不被异常值掩盖
        do_values = [r["dissolved_oxygen"] for r in wq if r["dissolved_oxygen"] is not None]
        self.assertIn(0, do_values)

        export = client.get("/api/water-quality-records/export/").text
        self.assertNotIn(",90,", export)  # 越界值不导出

        listing = client.get("/api/water-quality-records/", params={
            "batch_id": self.batch_id, "status": "valid",
        }).json()
        self.assertTrue(all(r["status"] == "valid" for r in listing))


# ---------------------------------------------------------------------------
# 旧库迁移：历史异常值隔离留痕、历史重复行归并，且不静默删除
# ---------------------------------------------------------------------------

class LegacyMigrationTest(unittest.TestCase):
    def test_legacy_anomalies_quarantined_and_duplicates_merged(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        engine = create_engine(f"sqlite:///{tmp.name}")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE ponds (id INTEGER PRIMARY KEY, name VARCHAR(100), area FLOAT, "
                "water_depth FLOAT, species VARCHAR(100), status VARCHAR(20), "
                "created_at DATETIME, updated_at DATETIME)"
            ))
            conn.execute(text(
                "CREATE TABLE batches (id INTEGER PRIMARY KEY, batch_number VARCHAR(50), "
                "pond_id INTEGER, species VARCHAR(100), stocking_date DATE, "
                "estimated_harvest_date DATE, actual_harvest_date DATE, status VARCHAR(20), "
                "created_at DATETIME, updated_at DATETIME)"
            ))
            conn.execute(text(
                "CREATE TABLE water_quality_records (id INTEGER PRIMARY KEY, batch_id INTEGER, "
                "record_date DATE, record_time VARCHAR(20), water_temperature FLOAT, ph_value FLOAT, "
                "dissolved_oxygen FLOAT, ammonia_nitrogen FLOAT, nitrite FLOAT, transparency FLOAT, "
                "notes TEXT, created_at DATETIME)"
            ))
            # 历史脏数据：pH=90、溶氧=无穷大；以及两条同采样重复
            conn.execute(text(
                "INSERT INTO water_quality_records "
                "(id, batch_id, record_date, record_time, water_temperature, ph_value, "
                "dissolved_oxygen, ammonia_nitrogen, nitrite, transparency, created_at) "
                "VALUES (1, 1, :d, '08:00', 25, 90, 1e999, NULL, NULL, NULL, '2024-01-01')"
            ), {"d": VALID_DAY})
            conn.execute(text(
                "INSERT INTO water_quality_records "
                "(id, batch_id, record_date, record_time, water_temperature, ph_value, "
                "dissolved_oxygen, ammonia_nitrogen, nitrite, transparency, created_at) "
                "VALUES (2, 1, :d, '08:00', 25, 90, 7, NULL, NULL, NULL, '2024-01-02')"
            ), {"d": VALID_DAY})
            conn.execute(text(
                "INSERT INTO water_quality_records "
                "(id, batch_id, record_date, record_time, water_temperature, ph_value, "
                "dissolved_oxygen, ammonia_nitrogen, nitrite, transparency, created_at) "
                "VALUES (3, 1, :d, '09:00', 26, 8, 7, NULL, NULL, 30, '2024-01-03')"
            ), {"d": VALID_DAY})

        run_lightweight_migrations(engine)

        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT id, status, quarantined_fields, ph_value, dissolved_oxygen, "
                "original_payload, merged_into_id FROM water_quality_records ORDER BY id"
            )).mappings().all()

        self.assertEqual(len(rows), 3)  # 一条都没被删除
        statuses = {r["id"]: r["status"] for r in rows}
        self.assertEqual(statuses[1], "quarantined")
        self.assertEqual(statuses[3], "valid")
        # 重复行之一被归并（保留较新 id=2）
        merged = [r for r in rows if r["merged_into_id"] is not None]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], 1)
        self.assertEqual(merged[0]["merged_into_id"], 2)

        bad = rows[0]
        self.assertIn("ph_value", json.loads(bad["quarantined_fields"]))
        self.assertIn("dissolved_oxygen", json.loads(bad["quarantined_fields"]))
        snapshot = json.loads(bad["original_payload"])
        self.assertEqual(snapshot["ph_value"], 90.0)  # 原值留存
        self.assertIsNone(bad["ph_value"])

        # 新列与部分唯一索引存在
        names = {ix["name"] for ix in inspect(engine).get_indexes("water_quality_records")}
        self.assertIn("uq_wq_sample_device", names)
        Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    # csv 用于导出断言
    import csv  # noqa: F401
    unittest.main()
