import React, { useEffect, useMemo, useState } from 'react';
import {
  Plus, Edit2, Trash2, X, Droplets, Thermometer, Gauge,
  AlertTriangle, Download, ShieldAlert, History, CheckCircle2,
} from 'lucide-react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, ReferenceLine,
} from 'recharts';
import axios from 'axios';
import { waterQualityRecordApi, batchApi } from '../services/api';
import type {
  WaterQualityRecord, Batch, WaterQualityFieldError,
} from '../types';

// 与后端 app/validation.py 的 METRIC_SPECS 保持一致
interface MetricDef {
  field: keyof Pick<WaterQualityRecord,
    'water_temperature' | 'ph_value' | 'dissolved_oxygen'
    | 'ammonia_nitrogen' | 'nitrite' | 'transparency'>;
  label: string;
  unit: string;
  min: number;
  max: number;
  step: string;
}

const METRICS: MetricDef[] = [
  { field: 'water_temperature', label: '水温', unit: '℃', min: 0, max: 40, step: '0.1' },
  { field: 'ph_value', label: 'pH值', unit: '', min: 0, max: 14, step: '0.1' },
  { field: 'dissolved_oxygen', label: '溶解氧', unit: 'mg/L', min: 0, max: 25, step: '0.01' },
  { field: 'ammonia_nitrogen', label: '氨氮', unit: 'mg/L', min: 0, max: 10, step: '0.01' },
  { field: 'nitrite', label: '亚硝酸盐', unit: 'mg/L', min: 0, max: 5, step: '0.01' },
  { field: 'transparency', label: '透明度', unit: 'cm', min: 0, max: 500, step: '1' },
];

// 溶解氧低氧警戒值（含 0 的真实低氧必须显著可见）
const LOW_DO_THRESHOLD = 3;

type FormState = {
  batch_id: string;
  record_date: string;
  record_time: string;
  [key: string]: string;
};

const EMPTY_FORM: FormState = {
  batch_id: '',
  record_date: '',
  record_time: '',
  water_temperature: '',
  ph_value: '',
  dissolved_oxygen: '',
  ammonia_nitrogen: '',
  nitrite: '',
  transparency: '',
  notes: '',
};

const FIELD_LABELS: Record<string, string> = {
  batch_id: '养殖批次',
  record_date: '检测日期',
  record_time: '检测时间',
  notes: '备注',
};
METRICS.forEach((m) => { FIELD_LABELS[m.field] = m.label; });

const extractApiErrors = (error: unknown): WaterQualityFieldError[] => {
  if (axios.isAxiosError(error) && error.response?.data) {
    const data = error.response.data as {
      errors?: WaterQualityFieldError[];
      detail?: Array<{ loc?: (string | number)[]; msg?: string }> | string;
    };
    if (Array.isArray(data.errors)) return data.errors;
    // FastAPI / pydantic 默认 422：detail[].loc 末位即字段名
    if (Array.isArray(data.detail)) {
      return data.detail.map((d) => {
        const loc = d.loc ?? [];
        const field = String(loc[loc.length - 1] ?? 'form');
        return { field, message: d.msg ?? '字段值不合法' };
      });
    }
    if (typeof data.detail === 'string') {
      return [{ field: 'form', message: data.detail }];
    }
  }
  return [{ field: 'form', message: '保存失败，请检查表单后重试' }];
};

const formatMetricCell = (value: number | null | undefined, quarantined: boolean, original: unknown) => {
  if (quarantined) {
    return (
      <span className="inline-flex items-center text-red-600 font-medium" title="异常值已隔离，待复核">
        <ShieldAlert size={13} className="mr-1" />
        {original === undefined || original === null || original === '' ? '异常' : String(original)}
      </span>
    );
  }
  if (value === null || value === undefined) {
    return <span className="text-gray-300">缺测</span>;
  }
  return <span>{value}</span>; // 真实 0 值正常显示为 0，与缺测严格区分
};

const WaterQuality: React.FC = () => {
  const [records, setRecords] = useState<WaterQualityRecord[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterBatchId, setFilterBatchId] = useState<string>('');
  const [chartMetric, setChartMetric] = useState<MetricDef['field']>('dissolved_oxygen');

  const [showModal, setShowModal] = useState(false);
  const [editingRecord, setEditingRecord] = useState<WaterQualityRecord | null>(null);
  const [formData, setFormData] = useState<FormState>(EMPTY_FORM);
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});

  const [reviewRecord, setReviewRecord] = useState<WaterQualityRecord | null>(null);
  const [reviewAction, setReviewAction] = useState<'corrected' | 'confirmed' | 'dismissed'>('corrected');
  const [reviewCorrections, setReviewCorrections] = useState<Record<string, string>>({});
  const [reviewNote, setReviewNote] = useState('');
  const [reviewer, setReviewer] = useState('管理员');
  const [reviewErrors, setReviewErrors] = useState<Record<string, string>>({});

  const fetchData = async () => {
    try {
      const [recordsRes, batchesRes] = await Promise.all([
        waterQualityRecordApi.getAll(),
        batchApi.getAll(),
      ]);
      setRecords(recordsRes.data);
      setBatches(batchesRes.data);
    } catch (error) {
      console.error('Error fetching data:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const getBatchNumber = (batchId: number) =>
    batches.find((b) => b.id === batchId)?.batch_number ?? '未知批次';

  const visibleRecords = useMemo(
    () => (filterBatchId
      ? records.filter((r) => r.batch_id === parseInt(filterBatchId))
      : records),
    [records, filterBatchId],
  );

  const validRecords = useMemo(
    () => visibleRecords.filter((r) => r.status === 'valid'),
    [visibleRecords],
  );
  const quarantinedRecords = useMemo(
    () => visibleRecords.filter((r) => r.status === 'quarantined'),
    [visibleRecords],
  );

  // 最新有效采样（异常值不能再“拉平”卡片与图表）
  const latestValid = validRecords.length > 0 ? validRecords[validRecords.length - 1] : null;
  const lowDo = latestValid?.dissolved_oxygen !== null
    && latestValid?.dissolved_oxygen !== undefined
    && latestValid.dissolved_oxygen <= LOW_DO_THRESHOLD;

  const chartData = useMemo(() =>
    validRecords.map((r) => {
      const metricValues: Record<string, number | null> = {};
      METRICS.forEach((m) => {
        const v = r[m.field];
        metricValues[m.field] = v === null || v === undefined ? null : Number(v);
      });
      return {
        id: r.id,
        label: `${r.record_date}${r.sample_time && r.sample_time !== '00:00' ? ` ${r.sample_time}` : ''}`,
        batch: getBatchNumber(r.batch_id),
        ...metricValues,
      };
    }), [validRecords]); // eslint-disable-line react-hooks/exhaustive-deps

  const validateForm = (): Record<string, string> => {
    const errors: Record<string, string> = {};
    if (!formData.batch_id) errors.batch_id = '请选择养殖批次';
    if (!formData.record_date) errors.record_date = '请选择检测日期';

    if (formData.record_time && !/^\d{2}:\d{2}$/.test(formData.record_time)) {
      errors.record_time = '时间格式应为 HH:MM';
    }

    // 上传窗口：采样时间不得晚于当前时间 +24 小时（与后端容差一致）
    if (formData.record_date) {
      const sample = new Date(`${formData.record_date}T${formData.record_time || '00:00'}:00`);
      const latest = new Date(Date.now() + 24 * 60 * 60 * 1000);
      if (sample.getTime() > latest.getTime()) {
        errors.record_date = '采样时间晚于合理上传窗口（当前时间 +24 小时），请核对日期';
      }
      if (sample.getFullYear() < 1990) errors.record_date = '采样日期过早，不能早于 1990 年';
    }

    METRICS.forEach((m) => {
      const raw = formData[m.field];
      if (raw === '') return; // 留空 = 缺测，合法
      const num = Number(raw);
      if (!Number.isFinite(num)) {
        errors[m.field] = `${m.label}必须是有限数值，不能是无穷大或非数字`;
      } else if (num < m.min || num > m.max) {
        errors[m.field] = `${m.label}有效范围 ${m.min}~${m.max}${m.unit ? ' ' + m.unit : ''}`;
      }
    });
    return errors;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const errors = validateForm();
    setFormErrors(errors);
    if (Object.keys(errors).length > 0) return;

    // 注意：空串 → null（缺测）；0 → 0（真实零值）。二者绝不能混用。
    const payload: Record<string, unknown> = {
      batch_id: parseInt(formData.batch_id),
      record_date: formData.record_date,
      record_time: formData.record_time || null,
      notes: formData.notes || null,
    };
    METRICS.forEach((m) => {
      payload[m.field] = formData[m.field] === '' ? null : Number(formData[m.field]);
    });

    try {
      if (editingRecord) {
        await waterQualityRecordApi.update(editingRecord.id, payload);
      } else {
        await waterQualityRecordApi.create(payload);
      }
      setShowModal(false);
      setEditingRecord(null);
      setFormData(EMPTY_FORM);
      setFormErrors({});
      fetchData();
    } catch (error) {
      const apiErrors = extractApiErrors(error);
      const mapped: Record<string, string> = {};
      apiErrors.forEach((err) => {
        mapped[err.field] = err.message;
      });
      setFormErrors(mapped);
    }
  };

  const openCreate = () => {
    setEditingRecord(null);
    setFormData(EMPTY_FORM);
    setFormErrors({});
    setShowModal(true);
  };

  const handleEdit = (record: WaterQualityRecord) => {
    setEditingRecord(record);
    setFormData({
      batch_id: record.batch_id.toString(),
      record_date: record.record_date,
      record_time: record.record_time || '',
      // 隔离字段当前值为 null，编辑框展示空（缺测态），原值在复核弹窗回看
      water_temperature: record.water_temperature?.toString() ?? '',
      ph_value: record.ph_value?.toString() ?? '',
      dissolved_oxygen: record.dissolved_oxygen?.toString() ?? '',
      ammonia_nitrogen: record.ammonia_nitrogen?.toString() ?? '',
      nitrite: record.nitrite?.toString() ?? '',
      transparency: record.transparency?.toString() ?? '',
      notes: record.notes || '',
    });
    setFormErrors({});
    setShowModal(true);
  };

  const handleDelete = async (id: number) => {
    if (!window.confirm('确定要删除这条有效记录吗？（隔离记录需先复核，不能直接删除）')) return;
    try {
      await waterQualityRecordApi.delete(id);
      fetchData();
    } catch (error) {
      window.alert(extractApiErrors(error).map((e) => e.message).join('\n'));
    }
  };

  const openReview = (record: WaterQualityRecord) => {
    setReviewRecord(record);
    setReviewAction('corrected');
    const corrections: Record<string, string> = {};
    record.quarantined_fields.forEach((f) => { corrections[f] = ''; });
    setReviewCorrections(corrections);
    setReviewNote(record.review_note || '');
    setReviewer(record.reviewed_by || '管理员');
    setReviewErrors({});
  };

  const submitReview = async () => {
    if (!reviewRecord) return;
    const errors: Record<string, string> = {};
    const correctionsPayload: Record<string, number> = {};
    if (reviewAction === 'corrected') {
      reviewRecord.quarantined_fields.forEach((f) => {
        const raw = reviewCorrections[f];
        if (raw === '' || raw === undefined) return; // 未填的字段保持隔离
        const num = Number(raw);
        const spec = METRICS.find((m) => m.field === f)!;
        if (!Number.isFinite(num)) {
          errors[f] = '更正值必须是有限数值';
        } else if (num < spec.min || num > spec.max) {
          errors[f] = `${spec.label}有效范围 ${spec.min}~${spec.max}`;
        } else {
          correctionsPayload[f] = num;
        }
      });
      if (Object.keys(correctionsPayload).length === 0) {
        errors.form = '选择“更正”时至少填写一个字段的更正值；不改值请选“确认异常”或“误报”';
      }
    }
    if (!reviewNote.trim()) errors.review_note = '请填写复核结论说明';
    setReviewErrors(errors);
    if (Object.keys(errors).length > 0) return;

    try {
      await waterQualityRecordApi.review(reviewRecord.id, {
        review_status: reviewAction,
        review_note: reviewNote,
        reviewed_by: reviewer,
        corrections: reviewAction === 'corrected' ? correctionsPayload : undefined,
      });
      setReviewRecord(null);
      fetchData();
    } catch (error) {
      const mapped: Record<string, string> = {};
      extractApiErrors(error).forEach((err) => { mapped[err.field] = err.message; });
      setReviewErrors(mapped);
    }
  };

  const metricInputClass = (field: string) =>
    `input-field ${formErrors[field] ? 'border-red-500 ring-1 ring-red-300 bg-red-50' : ''}`;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">加载中...</div>
      </div>
    );
  }

  const reviewStatusBadge = (status: string | null | undefined) => {
    if (!status) return <span className="badge badge-warning">待复核</span>;
    if (status === 'corrected') return <span className="badge badge-success">已更正</span>;
    if (status === 'confirmed') return <span className="badge badge-info">异常属实</span>;
    return <span className="badge">误报</span>;
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">水质监测</h1>
          <p className="text-gray-600 mt-1">按指标边界校验采样数据，异常值隔离复核，趋势与导出仅使用有效数据集</p>
        </div>
        <div className="flex items-center space-x-3">
          <a
            href={waterQualityRecordApi.exportUrl(filterBatchId ? parseInt(filterBatchId) : undefined)}
            className="btn-secondary flex items-center space-x-2"
          >
            <Download size={18} />
            <span>导出有效数据(CSV)</span>
          </a>
          <button onClick={openCreate} className="btn-primary flex items-center space-x-2">
            <Plus size={20} />
            <span>新增记录</span>
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card bg-blue-50">
          <div className="flex items-center space-x-3">
            <Thermometer className="text-blue-600" size={24} />
            <div>
              <p className="text-sm text-blue-600">最新有效水温</p>
              <p className="text-lg font-bold text-blue-700">
                {latestValid?.water_temperature !== null && latestValid?.water_temperature !== undefined
                  ? `${latestValid.water_temperature}℃` : '缺测'}
              </p>
            </div>
          </div>
        </div>
        <div className="card bg-green-50">
          <div className="flex items-center space-x-3">
            <Droplets className="text-green-600" size={24} />
            <div>
              <p className="text-sm text-green-600">最新有效pH值</p>
              <p className="text-lg font-bold text-green-700">
                {latestValid?.ph_value !== null && latestValid?.ph_value !== undefined
                  ? latestValid.ph_value : '缺测'}
              </p>
            </div>
          </div>
        </div>
        <div className={`card ${lowDo ? 'bg-red-50 border-2 border-red-300' : 'bg-purple-50'}`}>
          <div className="flex items-center space-x-3">
            <Gauge className={lowDo ? 'text-red-600' : 'text-purple-600'} size={24} />
            <div>
              <p className={`text-sm ${lowDo ? 'text-red-600' : 'text-purple-600'}`}>
                {lowDo ? '⚠ 溶解氧过低' : '最新有效溶解氧'}
              </p>
              <p className={`text-lg font-bold ${lowDo ? 'text-red-700' : 'text-purple-700'}`}>
                {latestValid?.dissolved_oxygen !== null && latestValid?.dissolved_oxygen !== undefined
                  ? `${latestValid.dissolved_oxygen} mg/L` : '缺测'}
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* 趋势图：仅有效数据集；缺测断线，不被异常值拉平 */}
      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <h2 className="text-lg font-semibold text-gray-900">有效数据趋势</h2>
          <div className="flex items-center space-x-3">
            <select
              value={filterBatchId}
              onChange={(e) => setFilterBatchId(e.target.value)}
              className="select-field w-48"
            >
              <option value="">全部批次</option>
              {batches.map((b) => (
                <option key={b.id} value={b.id}>{b.batch_number}</option>
              ))}
            </select>
            <select
              value={chartMetric}
              onChange={(e) => setChartMetric(e.target.value as MetricDef['field'])}
              className="select-field w-40"
            >
              {METRICS.map((m) => (
                <option key={m.field} value={m.field}>
                  {m.label}{m.unit ? `(${m.unit})` : ''}
                </option>
              ))}
            </select>
          </div>
        </div>
        {chartData.length === 0 ? (
          <p className="text-gray-500 text-center py-10">所选范围内暂无有效数据（隔离数据不参与趋势绘制）</p>
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={chartData} margin={{ top: 8, right: 24, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="label" tick={{ fontSize: 12 }} />
              <YAxis
                domain={['auto', 'auto']}
                tick={{ fontSize: 12 }}
                label={{
                  value: METRICS.find((m) => m.field === chartMetric)?.unit,
                  angle: -90, position: 'insideLeft',
                }}
              />
              <Tooltip
                formatter={(value, name) => {
                  const unit = METRICS.find((m) => m.field === chartMetric)?.unit;
                  const v = value as number | null;
                  return [v === null ? '缺测' : `${v} ${unit || ''}`, name];
                }}
              />
              <Legend />
              {chartMetric === 'dissolved_oxygen' && (
                <ReferenceLine
                  y={LOW_DO_THRESHOLD}
                  stroke="#dc2626"
                  strokeDasharray="6 4"
                  label={{ value: `低氧警戒 ${LOW_DO_THRESHOLD}`, fontSize: 11, fill: '#dc2626', position: 'insideTopRight' }}
                />
              )}
              {/* connectNulls=false：缺测时段断线呈现，不插值掩盖 */}
              <Line
                type="monotone"
                dataKey={chartMetric}
                name={METRICS.find((m) => m.field === chartMetric)?.label}
                stroke="#0e7490"
                strokeWidth={2}
                connectNulls={false}
                dot
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* 隔离待复核区 */}
      {quarantinedRecords.length > 0 && (
        <div className="card border-2 border-amber-300 bg-amber-50/40">
          <div className="flex items-center space-x-2 mb-4">
            <AlertTriangle className="text-amber-600" size={20} />
            <h2 className="text-lg font-semibold text-amber-900">
              异常隔离记录（{quarantinedRecords.length}）
            </h2>
            <span className="text-sm text-amber-700">原始录入已留痕，不进入趋势与导出，复核后可恢复</span>
          </div>
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>批次号</th><th>采样</th><th>隔离字段（原始录入）</th>
                  <th>复核状态</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {quarantinedRecords.map((record) => (
                  <tr key={record.id} className="bg-amber-50/60">
                    <td className="font-medium text-ocean-700">{getBatchNumber(record.batch_id)}</td>
                    <td>{record.record_date} {record.record_time || ''}</td>
                    <td>
                      <div className="flex flex-wrap gap-2">
                        {record.quarantined_fields.map((f) => (
                          <span key={f} className="inline-flex items-center px-2 py-0.5 rounded bg-red-100 text-red-700 text-sm">
                            <ShieldAlert size={12} className="mr-1" />
                            {FIELD_LABELS[f] || f}=
                            {record.original_payload?.[f] === undefined
                              || record.original_payload?.[f] === null
                              || record.original_payload?.[f] === ''
                              ? '非数值'
                              : String(record.original_payload[f])}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td>{reviewStatusBadge(record.review_status)}</td>
                    <td>
                      <div className="flex items-center space-x-2">
                        <button
                          onClick={() => openReview(record)}
                          className="btn-secondary text-sm py-1 flex items-center space-x-1"
                        >
                          <CheckCircle2 size={15} />
                          <span>复核</span>
                        </button>
                        <button
                          onClick={() => handleEdit(record)}
                          className="p-2 text-ocean-600 hover:bg-ocean-50 rounded-lg"
                          title="直接更正"
                        >
                          <Edit2 size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 有效记录列表 */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          有效记录（{validRecords.length}）
        </h2>
        <div className="overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>批次号</th>
                <th>检测日期</th>
                <th>时间</th>
                <th>水温(℃)</th>
                <th>pH值</th>
                <th>溶解氧(mg/L)</th>
                <th>氨氮(mg/L)</th>
                <th>亚硝酸盐(mg/L)</th>
                <th>透明度(cm)</th>
                <th>来源</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {validRecords.map((record) => (
                <tr key={record.id} className={record.dissolved_oxygen !== null
                  && record.dissolved_oxygen !== undefined
                  && record.dissolved_oxygen <= LOW_DO_THRESHOLD
                  ? 'bg-red-50' : ''}>
                  <td className="font-medium text-ocean-700">{getBatchNumber(record.batch_id)}</td>
                  <td>{record.record_date}</td>
                  <td>{record.record_time || <span className="text-gray-300">缺测</span>}</td>
                  <td>{formatMetricCell(record.water_temperature, false, null)}</td>
                  <td>{formatMetricCell(record.ph_value, false, null)}</td>
                  <td>
                    <span className={record.dissolved_oxygen !== null
                      && record.dissolved_oxygen !== undefined
                      && record.dissolved_oxygen <= LOW_DO_THRESHOLD
                      ? 'text-red-600 font-semibold' : ''}>
                      {formatMetricCell(record.dissolved_oxygen, false, null)}
                    </span>
                  </td>
                  <td>{formatMetricCell(record.ammonia_nitrogen, false, null)}</td>
                  <td>{formatMetricCell(record.nitrite, false, null)}</td>
                  <td>{formatMetricCell(record.transparency, false, null)}</td>
                  <td>
                    <span className="text-xs text-gray-500">
                      {record.source === 'sensor' ? `设备 ${record.device_id}` : '手工'}
                    </span>
                  </td>
                  <td>
                    <div className="flex items-center space-x-2">
                      <button
                        onClick={() => handleEdit(record)}
                        className="p-2 text-ocean-600 hover:bg-ocean-50 rounded-lg transition-colors"
                      >
                        <Edit2 size={18} />
                      </button>
                      <button
                        onClick={() => handleDelete(record.id)}
                        className="p-2 text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                      >
                        <Trash2 size={18} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {validRecords.length === 0 && (
                <tr>
                  <td colSpan={11} className="text-center py-8 text-gray-500">
                    暂无有效水质记录
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 新增 / 编辑弹窗 */}
      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-gray-900">
                {editingRecord ? '编辑水质监测记录' : '新增水质监测记录'}
              </h2>
              <button onClick={() => setShowModal(false)} className="p-2 text-gray-400 hover:text-gray-600">
                <X size={20} />
              </button>
            </div>

            {editingRecord?.status === 'quarantined' && (
              <div className="mb-4 p-3 bg-amber-100 text-amber-800 rounded-lg text-sm flex items-start space-x-2">
                <ShieldAlert size={16} className="mt-0.5 shrink-0" />
                <span>该记录含隔离字段，当前显示为缺测；化验员原始录入为
                  {editingRecord.quarantined_fields.map((f) =>
                    ` ${FIELD_LABELS[f]}=${String(editingRecord.original_payload?.[f] ?? '非数值')}`)}
                  。本次修改会以“更正”形式留痕，原值仍可回看。
                </span>
              </div>
            )}

            {formErrors.form && (
              <div className="mb-4 p-3 bg-red-100 text-red-700 rounded-lg text-sm">{formErrors.form}</div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4" noValidate>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  养殖批次 <span className="text-red-500">*</span>
                </label>
                <select
                  required
                  value={formData.batch_id}
                  onChange={(e) => setFormData({ ...formData, batch_id: e.target.value })}
                  className={`select-field ${formErrors.batch_id ? 'border-red-500 ring-1 ring-red-300 bg-red-50' : ''}`}
                >
                  <option value="">请选择批次</option>
                  {batches.map((batch) => (
                    <option key={batch.id} value={batch.id}>
                      {batch.batch_number} - {batch.species}
                    </option>
                  ))}
                </select>
                {formErrors.batch_id && <p className="text-red-600 text-xs mt-1">{formErrors.batch_id}</p>}
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    检测日期 <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="date"
                    required
                    value={formData.record_date}
                    onChange={(e) => setFormData({ ...formData, record_date: e.target.value })}
                    className={`input-field ${formErrors.record_date ? 'border-red-500 ring-1 ring-red-300 bg-red-50' : ''}`}
                  />
                  {formErrors.record_date && <p className="text-red-600 text-xs mt-1">{formErrors.record_date}</p>}
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">检测时间</label>
                  <input
                    type="time"
                    value={formData.record_time}
                    onChange={(e) => setFormData({ ...formData, record_time: e.target.value })}
                    className={metricInputClass('record_time')}
                  />
                  {formErrors.record_time && <p className="text-red-600 text-xs mt-1">{formErrors.record_time}</p>}
                </div>
              </div>

              <div className="grid grid-cols-3 gap-4">
                {METRICS.map((m) => (
                  <div key={m.field}>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      {m.label}{m.unit ? `(${m.unit})` : ''}
                    </label>
                    <input
                      type="number"
                      step={m.step}
                      value={formData[m.field]}
                      onChange={(e) => setFormData({ ...formData, [m.field]: e.target.value })}
                      className={metricInputClass(m.field)}
                      placeholder={`${m.min}~${m.max}，留空缺测`}
                      aria-invalid={!!formErrors[m.field]}
                    />
                    {formErrors[m.field] ? (
                      <p className="text-red-600 text-xs mt-1">{formErrors[m.field]}</p>
                    ) : (
                      <p className="text-gray-400 text-xs mt-1">范围 {m.min}~{m.max}；0 为实测零值</p>
                    )}
                  </div>
                ))}
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">备注</label>
                <textarea
                  value={formData.notes}
                  onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
                  className="input-field"
                  rows={2}
                  placeholder="备注信息"
                />
              </div>

              <div className="flex justify-end space-x-3 pt-4">
                <button type="button" onClick={() => setShowModal(false)} className="btn-secondary">
                  取消
                </button>
                <button type="submit" className="btn-primary">
                  {editingRecord ? '保存修改' : '创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 复核弹窗 */}
      {reviewRecord && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-gray-900 flex items-center space-x-2">
                <ShieldAlert className="text-amber-600" size={22} />
                <span>异常记录复核</span>
              </h2>
              <button onClick={() => setReviewRecord(null)} className="p-2 text-gray-400 hover:text-gray-600">
                <X size={20} />
              </button>
            </div>

            <p className="text-sm text-gray-600 mb-4">
              批次 {getBatchNumber(reviewRecord.batch_id)} · 采样 {reviewRecord.record_date} {reviewRecord.record_time || ''}
              {' '}· 来源 {reviewRecord.source === 'sensor' ? `设备 ${reviewRecord.device_id}` : '手工录入'}
            </p>

            <div className="space-y-3 mb-4">
              {reviewRecord.quarantined_fields.map((f) => {
                const spec = METRICS.find((m) => m.field === f)!;
                return (
                  <div key={f} className="p-3 bg-red-50 rounded-lg">
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-red-700 font-medium">
                        {spec.label} 原始录入：
                        <span className="font-bold ml-1">
                          {reviewRecord.original_payload?.[f] === undefined
                            || reviewRecord.original_payload?.[f] === null
                            || reviewRecord.original_payload?.[f] === ''
                            ? '非数值/无穷'
                            : String(reviewRecord.original_payload[f])}
                        </span>
                      </span>
                      <span className="text-gray-400">有效范围 {spec.min}~{spec.max} {spec.unit}</span>
                    </div>
                    {reviewAction === 'corrected' && (
                      <div className="mt-2">
                        <input
                          type="number"
                          step={spec.step}
                          value={reviewCorrections[f] || ''}
                          onChange={(e) => setReviewCorrections({ ...reviewCorrections, [f]: e.target.value })}
                          className={`input-field ${reviewErrors[f] ? 'border-red-500 ring-1 ring-red-300' : ''}`}
                          placeholder={`输入更正值（留空则该字段继续隔离）`}
                        />
                        {reviewErrors[f] && <p className="text-red-600 text-xs mt-1">{reviewErrors[f]}</p>}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="space-y-2 mb-4">
              {([
                ['corrected', '录入/化验错误，提交更正值恢复有效'],
                ['confirmed', '异常属实（真实极端值），维持隔离并记录结论'],
                ['dismissed', '误报（如探头故障），维持隔离并记录结论'],
              ] as const).map(([value, label]) => (
                <label key={value} className="flex items-center space-x-2 text-sm text-gray-700 cursor-pointer">
                  <input
                    type="radio"
                    checked={reviewAction === value}
                    onChange={() => setReviewAction(value)}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>

            <div className="grid grid-cols-3 gap-3 mb-4">
              <div className="col-span-1">
                <label className="block text-sm font-medium text-gray-700 mb-1">复核人</label>
                <input
                  type="text"
                  value={reviewer}
                  onChange={(e) => setReviewer(e.target.value)}
                  className="input-field"
                />
              </div>
              <div className="col-span-2">
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  复核结论 <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  value={reviewNote}
                  onChange={(e) => setReviewNote(e.target.value)}
                  className={`input-field ${reviewErrors.review_note ? 'border-red-500 ring-1 ring-red-300' : ''}`}
                  placeholder="如：化验员误将 pH 录为 90，复测为 8.2"
                />
                {reviewErrors.review_note && <p className="text-red-600 text-xs mt-1">{reviewErrors.review_note}</p>}
              </div>
            </div>
            {reviewErrors.form && <p className="text-red-600 text-sm mb-3">{reviewErrors.form}</p>}

            {/* 更正历史：传感器原值与人工更正都可回看 */}
            {reviewRecord.corrections.length > 0 && (
              <div className="mb-4">
                <h3 className="text-sm font-semibold text-gray-700 mb-2 flex items-center space-x-1">
                  <History size={14} />
                  <span>更正 / 归并历史</span>
                </h3>
                <div className="space-y-1 max-h-40 overflow-y-auto">
                  {reviewRecord.corrections.map((c) => (
                    <div key={c.id} className="text-xs text-gray-600 bg-gray-50 rounded p-2">
                      <span className="text-gray-400">{new Date(c.created_at).toLocaleString('zh-CN')}</span>
                      {' '}[{c.reason === 'merged' ? '重复归并' :
                        c.reason === 'confirmed' ? '确认异常' :
                        c.reason === 'dismissed' ? '判定误报' : '人工更正'}]
                      {' '}{FIELD_LABELS[c.field_name] || c.field_name}：
                      {c.old_value ?? '∅'} → {c.new_value ?? '保持隔离'}
                      {c.operator ? `（${c.operator}）` : ''}
                      {c.note ? ` ${c.note}` : ''}
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="flex justify-end space-x-3">
              <button onClick={() => setReviewRecord(null)} className="btn-secondary">取消</button>
              <button onClick={submitReview} className="btn-primary">提交复核结论</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default WaterQuality;
