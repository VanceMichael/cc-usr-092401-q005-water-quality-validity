from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
import json
from .database import Base

class Pond(Base):
    __tablename__ = "ponds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)
    area = Column(Float, nullable=False, comment="面积(亩)")
    water_depth = Column(Float, nullable=False, comment="水深(米)")
    species = Column(String(100), comment="养殖品种")
    status = Column(String(20), default="active", comment="状态: active, inactive")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batches = relationship("Batch", back_populates="pond")

class Batch(Base):
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_number = Column(String(50), unique=True, index=True, nullable=False, comment="批次号")
    pond_id = Column(Integer, ForeignKey("ponds.id"), nullable=False)
    species = Column(String(100), nullable=False, comment="养殖品种")
    stocking_date = Column(Date, nullable=False, comment="放苗日期")
    estimated_harvest_date = Column(Date, comment="预计收获日期")
    actual_harvest_date = Column(Date, comment="实际收获日期")
    status = Column(String(20), default="active", comment="状态: active, harvested, closed")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    pond = relationship("Pond", back_populates="batches")
    stocking_records = relationship("StockingRecord", back_populates="batch")
    feeding_records = relationship("FeedingRecord", back_populates="batch")
    water_quality_records = relationship("WaterQualityRecord", back_populates="batch")
    medication_records = relationship("MedicationRecord", back_populates="batch")
    cost_records = relationship("CostRecord", back_populates="batch")
    harvest_sales = relationship("HarvestSale", back_populates="batch")

class StockingRecord(Base):
    __tablename__ = "stocking_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    species = Column(String(100), nullable=False, comment="品种")
    quantity = Column(Integer, nullable=False, comment="数量(尾)")
    source = Column(String(200), comment="来源")
    batch_number = Column(String(50), comment="苗种批次号")
    weight_per_unit = Column(Float, comment="单重(克/尾)")
    total_weight = Column(Float, comment="总重量(公斤)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="stocking_records")

class FeedingRecord(Base):
    __tablename__ = "feeding_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    feeding_date = Column(Date, nullable=False, comment="投喂日期")
    feed_type = Column(String(100), nullable=False, comment="饲料类型")
    feed_quantity = Column(Float, nullable=False, comment="投喂量(公斤)")
    feeding_time = Column(String(20), comment="投喂时间")
    weather = Column(String(50), comment="天气情况")
    water_temperature = Column(Float, comment="水温(℃)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="feeding_records")

class WaterQualityRecord(Base):
    __tablename__ = "water_quality_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    record_date = Column(Date, nullable=False, comment="检测日期")
    record_time = Column(String(20), comment="检测时间(HH:MM)")
    # 归一化的采样时刻（HH:MM，空按 00:00），仅用于幂等键与排序
    sample_time = Column(String(5), nullable=False, default="00:00", comment="采样时刻(幂等键)")
    water_temperature = Column(Float, comment="水温(℃)")
    ph_value = Column(Float, comment="pH值")
    dissolved_oxygen = Column(Float, comment="溶解氧(mg/L)")
    ammonia_nitrogen = Column(Float, comment="氨氮(mg/L)")
    nitrite = Column(Float, comment="亚硝酸盐(mg/L)")
    transparency = Column(Float, comment="透明度(cm)")
    notes = Column(Text, comment="备注")

    # ---- 数据来源与质量状态 ----
    device_id = Column(String(64), nullable=False, default="manual", comment="采集设备标识，手工录入为 manual")
    source = Column(String(20), nullable=False, default="manual", comment="来源: manual 手工, sensor 设备")
    # valid 全部字段有效；quarantined 含被隔离字段（待复核）；
    # rejected 整单时间/批次不合法被拒收但仍留痕
    status = Column(String(20), nullable=False, default="valid", index=True,
                    comment="质量状态: valid, quarantined, rejected")
    quarantined_fields = Column(Text, default="[]", comment="被隔离字段名(JSON数组)")
    original_payload = Column(Text, comment="首次上报的原始报文(JSON)，异常值留痕不删除")
    # ---- 复核结论 ----
    review_status = Column(String(20), nullable=True, comment="复核结论: confirmed 异常属实, corrected 已更正, dismissed 录入误报")
    review_note = Column(Text, comment="复核说明")
    reviewed_by = Column(String(100), comment="复核人")
    reviewed_at = Column(DateTime, comment="复核时间")
    # 归并追踪：被哪条记录幂等归并 / 归并自哪个设备上报标识
    merged_into_id = Column(Integer, ForeignKey("water_quality_records.id"), nullable=True)
    client_ref = Column(String(100), nullable=True, index=True, comment="设备/客户端上报唯一标识，用于幂等")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("Batch", back_populates="water_quality_records")
    corrections = relationship(
        "WaterQualityCorrection", back_populates="record",
        cascade="all, delete-orphan", order_by="WaterQualityCorrection.created_at"
    )

    @property
    def quarantined_fields_list(self):
        try:
            return json.loads(self.quarantined_fields or "[]")
        except (ValueError, TypeError):
            return []

    @quarantined_fields_list.setter
    def quarantined_fields_list(self, value):
        self.quarantined_fields = json.dumps(list(value or []), ensure_ascii=False)


class WaterQualityCorrection(Base):
    """水质记录更正 / 复核留痕。

    人工更正不覆盖传感器原值：每次改动追加一行，原值与新值都可回看。
    """
    __tablename__ = "water_quality_corrections"

    id = Column(Integer, primary_key=True, index=True)
    record_id = Column(Integer, ForeignKey("water_quality_records.id"), nullable=False, index=True)
    field_name = Column(String(50), nullable=False, comment="被更正字段，record_date 表示整单时间更正")
    old_value = Column(Text, comment="更正前原始值（异常录入原样保留）")
    new_value = Column(Text, comment="更正后的值")
    reason = Column(String(20), nullable=False, default="corrected",
                    comment="类型: corrected 人工更正, confirmed 确认异常, dismissed 判定误报")
    operator = Column(String(100), comment="操作人")
    note = Column(Text, comment="复核/更正说明")
    created_at = Column(DateTime, default=datetime.utcnow)

    record = relationship("WaterQualityRecord", back_populates="corrections")

class MedicationRecord(Base):
    __tablename__ = "medication_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    medication_date = Column(Date, nullable=False, comment="用药日期")
    drug_name = Column(String(200), nullable=False, comment="药品名称")
    drug_type = Column(String(50), comment="药品类型")
    dosage = Column(Float, comment="用量")
    dosage_unit = Column(String(20), default="kg", comment="用量单位")
    administration_method = Column(String(100), comment="施用方法")
    purpose = Column(String(200), comment="用途")
    manufacturer = Column(String(200), comment="生产厂家")
    batch_number = Column(String(50), comment="药品批次号")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="medication_records")

class CostRecord(Base):
    __tablename__ = "cost_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    cost_date = Column(Date, nullable=False, comment="费用日期")
    cost_type = Column(String(50), nullable=False, comment="费用类型: feed, medicine, labor, electricity, other")
    amount = Column(Float, nullable=False, comment="金额(元)")
    description = Column(String(500), comment="费用描述")
    quantity = Column(Float, comment="数量")
    unit = Column(String(20), comment="单位")
    unit_price = Column(Float, comment="单价")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="cost_records")

class HarvestSale(Base):
    __tablename__ = "harvest_sales"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    sale_date = Column(Date, nullable=False, comment="销售日期")
    weight = Column(Float, nullable=False, comment="重量(公斤)")
    unit_price = Column(Float, nullable=False, comment="单价(元/公斤)")
    total_amount = Column(Float, comment="总金额(元)")
    buyer = Column(String(200), comment="买家")
    batch_number = Column(String(50), comment="追溯批次号")
    quality_grade = Column(String(50), comment="质量等级")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="harvest_sales")
