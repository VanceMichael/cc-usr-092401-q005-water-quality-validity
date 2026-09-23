import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Plus, Edit2, Trash2, X, Droplets, Thermometer, Gauge,
  AlertTriangle, CheckCircle, Download, History, ShieldAlert, Activity, XCircle,
} from 'lucide-react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import axios from 'axios';
import { waterQualityRecordApi, batchApi } from '../services/api';
import type {
  WaterQualityRecord, Batch, FieldErrors, WaterQualityMetricKey,
  WaterQualityTrendPoint, WaterQualityRevision,
} from '../types';

interface MetricMeta {
  key: WaterQualityMetricKey;
  label: string;
  unit: string;
  min: number;
  max: number;
  step: string;
  color: string;
}

// 与后端 validation.METRIC_SPECS 保持一致的指标边界（用于前端即时提示）
const METRICS: MetricMeta[] = [
  { key: 'water_temperature', label: '水温', unit: '℃', min: -2, max: 45, step: '0.1', color: '#eb6834' },
  { key: 'ph_value', label: '酸碱度(pH)', unit: '', min: 0, max: 14, step: '0.1', color: '#2a78d6' },
  { key: 'dissolved_oxygen', label: '溶解氧', unit: 'mg/L', min: 0, max: 25, step: '0.01', color: '#1baf7a' },
  { key: 'ammonia_nitrogen', label: '氨氮', unit: 'mg/L', min: 0, max: 10, step: '0.01', color: '#eda100' },
  { key: 'nitrite', label: '亚硝酸盐', unit: 'mg/L', min: 0, max: 5, step: '0.01', color: '#e87ba4' },
  { key: 'transparency', label: '透明度', unit: 'cm', min: 0, max: 500, step: '1', color: '#4a3aa7' },
];

const EMPTY_FORM = {
  batch_id: '',
  record_date: '',
  record_time: '',
  device_id: '',
  water_temperature: '',
  ph_value: '',
  dissolved_oxygen: '',
  ammonia_nitrogen: '',
  nitrite: '',
  transparency: '',
  notes: '',
};

type FormState = typeof EMPTY_FORM;

const ACTION_LABEL: Record<string, string> = {
  create: '录入原值',
  update: '人工更正',
  merge: '重复归并',
  review: '复核',
};

function extractFieldErrors(err: unknown): FieldErrors {
  if (axios.isAxiosError(err) && err.response?.data?.detail?.field_errors) {
    return err.response.data.detail.field_errors as FieldErrors;
  }
  return { form: '保存失败，请检查网络或稍后重试' };
}

function isSoftOnly(errors: FieldErrors): boolean {
  // 非有限数等硬性错误不能隔离；这里只按文案特征粗判，后端会再次把关
  return !Object.values(errors).some((msg) => msg?.includes('有限数值') || msg?.includes('必须是数值'));
}

/** 缺测显示为「缺测」，零值显示为 0 —— 二者严格区分。 */
const MetricCell: React.FC<{ value?: number | null }> = ({ value }) => {
  if (value === null || value === undefined) {
    return <span className="text-gray-400 text-xs">缺测</span>;
  }
  return <span className={value === 0 ? 'font-medium text-gray-900' : ''}>{value}</span>;
};

function formatSampledAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const TrendTooltip = ({ active, payload, label, unit, title }: any) => {
  if (!active || !payload?.length) return null;
  const value = payload[0]?.value;
  return (
    <div className="bg-white border border-gray-200 rounded-lg shadow px-3 py-2 text-xs">
      <p className="text-gray-500">{title} · {label}</p>
      <p className="font-semibold text-gray-900">
        {value === null || value === undefined ? '缺测' : `${value}${unit ? ` ${unit}` : ''}`}
      </p>
    </div>
  );
};

const WaterQuality: React.FC = () => {
  const [tab, setTab] = useState<'valid' | 'quarantine' | 'rejected'>('valid');
  const [records, setRecords] = useState<WaterQualityRecord[]>([]);
  const [quarantined, setQuarantined] = useState<WaterQualityRecord[]>([]);
  const [rejected, setRejected] = useState<WaterQualityRecord[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [trendPoints, setTrendPoints] = useState<WaterQualityTrendPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [batchFilter, setBatchFilter] = useState<string>('');
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [showModal, setShowModal] = useState(false);
  const [editingRecord, setEditingRecord] = useState<WaterQualityRecord | null>(null);
  const [formData, setFormData] = useState<FormState>(EMPTY_FORM);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [quarantineOffer, setQuarantineOffer] = useState(false);

  const [reviewRecord, setReviewRecord] = useState<WaterQualityRecord | null>(null);
  const [reviewConclusion, setReviewConclusion] = useState('');
  const [reviewer, setReviewer] = useState('');
  const [corrections, setCorrections] = useState<Record<string, string>>({});
  const [reviewErrors, setReviewErrors] = useState<FieldErrors>({});
  const [reviewSaving, setReviewSaving] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const filterId = batchFilter ? parseInt(batchFilter) : undefined;
      const [validRes, quarRes, rejRes, batchesRes, trendRes] = await Promise.all([
        waterQualityRecordApi.getAll(filterId, 'valid'),
        waterQualityRecordApi.getAll(filterId, 'quarantined'),
        waterQualityRecordApi.getAll(filterId, 'rejected'),
        batchApi.getAll(),
        waterQualityRecordApi.trend(filterId),
      ]);
      setRecords(validRes.data);
      setQuarantined(quarRes.data);
      setRejected(rejRes.data);
      setBatches(batchesRes.data);
      setTrendPoints(trendRes.data.points);
    } catch (error) {
      console.error('Error fetching data:', error);
    } finally {
      setLoading(false);
    }
  }, [batchFilter]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const getBatchNumber = (batchId: number) =>
    batches.find((b) => b.id === batchId)?.batch_number ?? '未知批次';

  const latestPoint = trendPoints[trendPoints.length - 1];

  const buildPayload = () => {
    const parse = (raw: string): number | undefined => {
      if (raw.trim() === '') return undefined; // 缺测
      return parseFloat(raw); // "0" -> 0，与缺测严格区分
    };
    return {
      batch_id: parseInt(formData.batch_id),
      record_date: formData.record_date,
      record_time: formData.record_time || null,
      device_id: formData.device_id.trim() || 'manual',
      water_temperature: parse(formData.water_temperature),
      ph_value: parse(formData.ph_value),
      dissolved_oxygen: parse(formData.dissolved_oxygen),
      ammonia_nitrogen: parse(formData.ammonia_nitrogen),
      nitrite: parse(formData.nitrite),
      transparency: parse(formData.transparency),
      notes: formData.notes || null,
    };
  };

  const clientValidate = (): FieldErrors => {
    const errors: FieldErrors = {};
    if (!formData.batch_id) errors.batch_id = '请选择养殖批次';
    if (!formData.record_date) errors.record_date = '请选择检测日期';
    if (formData.record_time && !/^([01]\d|2[0-3]):[0-5]\d$/.test(formData.record_time)) {
      errors.record_time = '时间格式应为 HH:MM';
    }
    for (const m of METRICS) {
      const raw = formData[m.key];
      if (raw.trim() === '') continue;
      const num = Number(raw);
      if (!Number.isFinite(num)) {
        errors[m.key] = `${m.label}必须是有限数值，不能为无穷大或 NaN`;
      } else if (num < m.min || num > m.max) {
        errors[m.key] = `${m.label}应在 ${m.min}~${m.max}${m.unit} 之间`;
      }
    }
    return errors;
  };

  const submitForm = async (acceptQuarantine = false) => {
    const localErrors = clientValidate();
    if (Object.keys(localErrors).length > 0) {
      setFieldErrors(localErrors);
      setQuarantineOffer(false);
      return;
    }
    try {
      const payload = buildPayload();
      const res = editingRecord
        ? await waterQualityRecordApi.update(editingRecord.id, payload)
        : await waterQualityRecordApi.create(payload, acceptQuarantine);

      if (!editingRecord && res.data.merged_from_duplicate) {
        setNotice(`同一设备在该采样时刻已有记录，已幂等归并到 #${res.data.id}，未产生重复数据`);
      } else {
        setNotice(null);
      }
      setShowModal(false);
      setEditingRecord(null);
      setFormData(EMPTY_FORM);
      setFieldErrors({});
      setQuarantineOffer(false);
      fetchData();
    } catch (error) {
      const errors = extractFieldErrors(error);
      setFieldErrors(errors);
      setQuarantineOffer(!editingRecord && acceptQuarantine === false && isSoftOnly(errors));
    }
  };

  const openCreate = () => {
    setEditingRecord(null);
    setFormData(EMPTY_FORM);
    setFieldErrors({});
    setQuarantineOffer(false);
    setShowModal(true);
  };

  const openEdit = (record: WaterQualityRecord) => {
    setEditingRecord(record);
    setFormData({
      batch_id: String(record.batch_id),
      record_date: record.record_date,
      record_time: record.record_time || '',
      device_id: record.device_id || 'manual',
      water_temperature: record.water_temperature === null || record.water_temperature === undefined
        ? '' : String(record.water_temperature),
      ph_value: record.ph_value ?? '',
      dissolved_oxygen: record.dissolved_oxygen ?? '',
      ammonia_nitrogen: record.ammonia_nitrogen ?? '',
      nitrite: record.nitrite ?? '',
      transparency: record.transparency ?? '',
      notes: record.notes || '',
    } as Record<keyof FormState, string>);
    setFieldErrors({});
    setQuarantineOffer(false);
    setShowModal(true);
  };

  const handleDelete = async (record: WaterQualityRecord) => {
    if (!window.confirm(`确定删除 #${record.id} 这条有效记录吗？异常记录受删除保护。`)) return;
    try {
      await waterQualityRecordApi.delete(record.id);
      fetchData();
    } catch (error) {
      window.alert(Object.values(extractFieldErrors(error))[0] ?? '删除失败');
    }
  };

  const openReview = (record: WaterQualityRecord) => {
    setReviewRecord(record);
    setReviewConclusion('');
    setReviewer('');
    setCorrections({});
    setReviewErrors({});
  };

  const submitReview = async (action: 'confirm_valid' | 'confirm_invalid') => {
    if (!reviewRecord) return;
    if (!reviewConclusion.trim()) {
      setReviewErrors({ form: '请填写复核结论（异常值不得静默处理）' });
      return;
    }
    setReviewSaving(true);
    try {
      const correctionPayload: Partial<Record<WaterQualityMetricKey, number>> = {};
      for (const m of METRICS) {
        const raw = corrections[m.key];
        if (raw !== undefined && raw.trim() !== '') {
          correctionPayload[m.key] = parseFloat(raw);
        }
      }
      await waterQualityRecordApi.review(reviewRecord.id, {
        action,
        conclusion: reviewConclusion.trim(),
        reviewed_by: reviewer.trim() || 'reviewer',
        ...(action === 'confirm_valid' ? { corrections: correctionPayload } : {}),
      });
      setReviewRecord(null);
      fetchData();
    } catch (error) {
      setReviewErrors(extractFieldErrors(error));
    } finally {
      setReviewSaving(false);
    }
  };

  const chartData = useMemo(
    () => trendPoints.map((p) => ({ ...p, label: formatSampledAt(p.sampled_at) })),
    [trendPoints]
  );

  const renderFieldError = (field: string) =>
    fieldErrors[field] ? (
      <p className="mt-1 text-xs text-red-600 flex items-start gap-1">
        <AlertTriangle size={12} className="mt-0.5 shrink-0" />
        <span>{fieldErrors[field]}</span>
      </p>
    ) : null;

  const invalidReasonKeys = (record: WaterQualityRecord): string[] =>
    Object.keys(record.invalid_field_reasons ?? {});

  if (loading) {
    return <div className="flex items-center justify-center h-64"><div className="text-gray-500">加载中...</div></div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">水质监测</h1>
          <p className="text-gray-600 mt-1">按指标边界校验 · 异常值隔离复核 · 趋势与导出共用有效数据集</p>
        </div>
        <button onClick={openCreate} className="btn-primary flex items-center space-x-2">
          <Plus size={20} />
          <span>新增记录</span>
        </button>
      </div>

      {notice && (
        <div className="p-3 bg-blue-50 text-blue-800 rounded-lg flex items-center gap-2 text-sm">
          <CheckCircle size={16} />
          <span>{notice}</span>
          <button className="ml-auto text-blue-500" onClick={() => setNotice(null)}><X size={16} /></button>
        </div>
      )}

      {/* 过滤行 */}
      <div className="card flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-600">批次</span>
          <select
            value={batchFilter}
            onChange={(e) => setBatchFilter(e.target.value)}
            className="select-field max-w-[260px]"
          >
            <option value="">全部批次</option>
            {batches.map((b) => (
              <option key={b.id} value={b.id}>{b.batch_number} - {b.species}</option>
            ))}
          </select>
        </div>
        <a
          href={waterQualityRecordApi.exportUrl(batchFilter ? parseInt(batchFilter) : undefined)}
          className="btn-secondary flex items-center gap-2 text-sm"
        >
          <Download size={16} />
          <span>导出有效数据 CSV</span>
        </a>
        <div className="flex rounded-lg border border-gray-200 overflow-hidden ml-auto">
          <button
            onClick={() => setTab('valid')}
            className={`px-4 py-2 text-sm flex items-center gap-1 ${tab === 'valid' ? 'bg-ocean-600 text-white' : 'bg-white text-gray-600'}`}
          >
            <Activity size={15} /> 有效数据 ({records.length})
          </button>
          <button
            onClick={() => setTab('quarantine')}
            className={`px-4 py-2 text-sm flex items-center gap-1 ${tab === 'quarantine' ? 'bg-amber-500 text-white' : 'bg-white text-gray-600'}`}
          >
            <ShieldAlert size={15} /> 隔离待复核 ({quarantined.length})
          </button>
          <button
            onClick={() => setTab('rejected')}
            className={`px-4 py-2 text-sm flex items-center gap-1 ${tab === 'rejected' ? 'bg-red-600 text-white' : 'bg-white text-gray-600'}`}
          >
            <XCircle size={15} /> 已作废 ({rejected.length})
          </button>
        </div>
      </div>

      {/* 最新值卡片：来自趋势同一有效数据集 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {([
          { m: METRICS[0], icon: Thermometer, bg: 'bg-orange-50', fg: 'text-orange-600', bold: 'text-orange-700' },
          { m: METRICS[1], icon: Droplets, bg: 'bg-blue-50', fg: 'text-blue-600', bold: 'text-blue-700' },
          { m: METRICS[2], icon: Gauge, bg: 'bg-green-50', fg: 'text-green-600', bold: 'text-green-700' },
        ] as const).map(({ m, icon: Icon, bg, fg, bold }) => (
          <div key={m.key} className={`card ${bg}`}>
            <div className="flex items-center space-x-3">
              <Icon className={fg} size={24} />
              <div>
                <p className={`text-sm ${fg}`}>{m.label}{m.unit ? `(${m.unit})` : ''}</p>
                <p className={`text-lg font-bold ${bold}`}>
                  {latestPlanPoint(latestPoint, m.key) !== '' ? (
                    <>{latestPlanPoint(latestPoint, m.key)} {m.unit}</>
                  ) : (
                    <span className="text-gray-400 text-sm">缺测</span>
                  )}
                </p>
              </div>
            </div>
          </div>
        ))}
      </div>

      {tab === 'valid' && (
        <>
          {/* 趋势小多图：每个指标独立坐标轴，缺测处断线 */}
          <div className="card">
            <h2 className="text-lg font-semibold text-gray-900 mb-1">有效数据趋势</h2>
            <p className="text-sm text-gray-500 mb-4">
              仅绘制有效记录；断线处为缺测（非零值），异常隔离记录不会拉平曲线。
            </p>
            {chartData.length === 0 ? (
              <div className="h-40 flex items-center justify-center text-gray-400 text-sm">暂无有效数据</div>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-5">
                {METRICS.map((m) => (
                  <div key={m.key} className="border border-gray-100 rounded-xl p-3">
                    <p className="text-sm font-medium text-gray-700 mb-2">
                      {m.label}{m.unit ? ` (${m.unit})` : ''}
                    </p>
                    <ResponsiveContainer width="100%" height={170}>
                      <LineChart data={chartData} margin={{ top: 4, right: 12, bottom: 0, left: -8 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#eef0f2" />
                        <XAxis
                          dataKey="label"
                          tick={{ fontSize: 10, fill: '#8a8f98' }}
                          minTickGap={24}
                          tickLine={false}
                        />
                        <YAxis
                          domain={['auto', 'auto']}
                          tick={{ fontSize: 10, fill: '#8a8f98' }}
                          tickLine={false}
                          axisLine={false}
                        />
                        <Tooltip
                          content={<TrendTooltip unit={m.unit} title={m.label} />}
                          cursor={{ stroke: '#c7ccd3' }}
                        />
                        <Line
                          type="monotone"
                          dataKey={m.key}
                          stroke={m.color}
                          strokeWidth={2}
                          dot={{ r: 3, strokeWidth: 0 }}
                          activeDot={{ r: 5 }}
                          connectNulls={false}
                          isAnimationActive={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="card">
            <div className="overflow-x-auto">
              <table className="table">
                <thead>
                  <tr>
                    <th>批次号</th>
                    <th>检测日期</th>
                    <th>时间</th>
                    <th>设备</th>
                    {METRICS.map((m) => <th key={m.key}>{m.label}{m.unit ? `(${m.unit})` : ''}</th>)}
                    <th>留痕</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((record) => (
                    <React.Fragment key={record.id}>
                      <tr>
                        <td className="font-medium text-ocean-700">{getBatchNumber(record.batch_id)}</td>
                        <td>{record.record_date}</td>
                        <td>{record.record_time || <span className="text-gray-400">-</span>}</td>
                        <td className="text-xs text-gray-500">{record.device_id || 'manual'}</td>
                        {METRICS.map((m) => (
                          <td key={m.key}><MetricCell value={record[m.key]} /></td>
                        ))}
                        <td>
                          <button
                            className="p-1.5 text-gray-500 hover:bg-gray-100 rounded-lg"
                            title="查看修订历史（传感器原值/人工更正）"
                            onClick={() => setExpandedId(expandedId === record.id ? null : record.id)}
                          >
                            <History size={16} />
                          </button>
                        </td>
                        <td>
                          <div className="flex items-center space-x-2">
                            <button onClick={() => openEdit(record)} className="p-2 text-ocean-600 hover:bg-ocean-50 rounded-lg">
                              <Edit2 size={18} />
                            </button>
                            <button onClick={() => handleDelete(record)} className="p-2 text-red-600 hover:bg-red-50 rounded-lg">
                              <Trash2 size={18} />
                            </button>
                          </div>
                        </td>
                      </tr>
                      {expandedId === record.id && (
                        <tr>
                          <td colSpan={12} className="bg-gray-50">
                            <RevisionHistory revisions={record.revisions ?? []} />
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  ))}
                  {records.length === 0 && (
                    <tr><td colSpan={12} className="text-center py-8 text-gray-500">暂无有效水质记录</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {tab === 'quarantine' && (
        <div className="card">
          <div className="flex items-center gap-2 mb-4 text-amber-700">
            <ShieldAlert size={20} />
            <h2 className="text-lg font-semibold">隔离记录（不进入趋势、导出与分析，等待复核）</h2>
          </div>
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>批次号</th>
                  <th>采样时间</th>
                  <th>设备</th>
                  <th>异常字段与原因</th>
                  <th>当前值</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {quarantined.map((record) => (
                  <React.Fragment key={record.id}>
                    <tr className="bg-amber-50/40">
                      <td className="font-medium text-ocean-700">{getBatchNumber(record.batch_id)}</td>
                      <td>{record.record_date} {record.record_time || ''}</td>
                      <td className="text-xs text-gray-500">{record.device_id || 'manual'}</td>
                      <td>
                        <ul className="space-y-1">
                          {invalidReasonKeys(record).map((f) => (
                            <li key={f} className="text-xs text-red-700">
                              <span className="font-medium">
                                {METRICS.find((m) => m.key === f)?.label
                                  ?? (f === 'record_date' ? '检测日期' : f === 'record_time' ? '检测时间' : f)}：
                              </span>
                              {record.invalid_field_reasons?.[f]}
                            </li>
                          ))}
                        </ul>
                      </td>
                      <td>
                        <div className="text-xs text-gray-600 space-y-0.5">
                          {METRICS.filter((m) => invalidReasonKeys(record).includes(m.key)).map((m) => (
                            <div key={m.key}>
                              {m.label}: <MetricCell value={record[m.key]} />
                            </div>
                          ))}
                        </div>
                      </td>
                      <td>
                        <div className="flex gap-2">
                          <button
                            onClick={() => openReview(record)}
                            className="btn-secondary text-xs py-1.5 px-3 flex items-center gap-1"
                          >
                            <CheckCircle size={14} /> 复核
                          </button>
                          <button
                            onClick={() => openEdit(record)}
                            className="btn-secondary text-xs py-1.5 px-3 flex items-center gap-1"
                            title="直接修改字段，保存通过校验后自动恢复有效"
                          >
                            <Edit2 size={14} /> 更正
                          </button>
                          <button
                            className="p-2 text-gray-400 hover:bg-gray-100 rounded-lg"
                            title="查看留痕"
                            onClick={() => setExpandedId(expandedId === record.id ? null : record.id)}
                          >
                            <History size={16} />
                          </button>
                        </div>
                      </td>
                    </tr>
                    {expandedId === record.id && (
                      <tr>
                        <td colSpan={6} className="bg-gray-50">
                          <RevisionHistory revisions={record.revisions ?? []} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
                {quarantined.length === 0 && (
                  <tr><td colSpan={6} className="text-center py-8 text-gray-500">没有待复核的隔离记录</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === 'rejected' && (
        <div className="card">
          <div className="flex items-center gap-2 mb-4 text-red-700">
            <XCircle size={20} />
            <h2 className="text-lg font-semibold">已作废记录（只读保留，可回看原值与复核结论）</h2>
          </div>
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>批次号</th>
                  <th>采样时间</th>
                  <th>设备</th>
                  {METRICS.map((m) => <th key={m.key}>{m.label}</th>)}
                  <th>复核结论 / 复核人</th>
                  <th>留痕</th>
                </tr>
              </thead>
              <tbody>
                {rejected.map((record) => (
                  <React.Fragment key={record.id}>
                    <tr className="text-gray-500">
                      <td>{getBatchNumber(record.batch_id)}</td>
                      <td>{record.record_date} {record.record_time || ''}</td>
                      <td className="text-xs">{record.device_id || 'manual'}</td>
                      {METRICS.map((m) => (
                        <td key={m.key}><MetricCell value={record[m.key]} /></td>
                      ))}
                      <td className="max-w-xs">
                        <p className="text-xs text-gray-700">{record.review_conclusion}</p>
                        <p className="text-xs text-gray-400 mt-0.5">
                          {record.reviewed_by}
                          {record.reviewed_at ? ` · ${new Date(record.reviewed_at).toLocaleString('zh-CN')}` : ''}
                        </p>
                      </td>
                      <td>
                        <button
                          className="p-2 text-gray-400 hover:bg-gray-100 rounded-lg"
                          onClick={() => setExpandedId(expandedId === record.id ? null : record.id)}
                        >
                          <History size={16} />
                        </button>
                      </td>
                    </tr>
                    {expandedId === record.id && (
                      <tr>
                        <td colSpan={12} className="bg-gray-50">
                          <RevisionHistory revisions={record.revisions ?? []} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
                {rejected.length === 0 && (
                  <tr><td colSpan={12} className="text-center py-8 text-gray-500">没有已作废记录</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 新增/编辑弹窗 */}
      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-gray-900">
                {editingRecord ? `人工更正 #${editingRecord.id}` : '新增水质记录'}
              </h2>
              <button onClick={() => setShowModal(false)} className="p-2 text-gray-400 hover:text-gray-600">
                <X size={20} />
              </button>
            </div>

            {fieldErrors.form && (
              <div className="mb-4 p-3 bg-red-50 text-red-700 rounded-lg text-sm">{fieldErrors.form}</div>
            )}

            <form
              onSubmit={(e) => {
                e.preventDefault();
                submitForm(false);
              }}
              className="space-y-4"
            >
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  养殖批次 <span className="text-red-500">*</span>
                </label>
                <select
                  value={formData.batch_id}
                  onChange={(e) => setFormData({ ...formData, batch_id: e.target.value })}
                  className={`select-field ${fieldErrors.batch_id ? 'border-red-400 ring-1 ring-red-300' : ''}`}
                >
                  <option value="">请选择批次</option>
                  {batches.map((batch) => (
                    <option key={batch.id} value={batch.id}>
                      {batch.batch_number} - {batch.species}
                    </option>
                  ))}
                </select>
                {renderFieldError('batch_id')}
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    检测日期 <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="date"
                    value={formData.record_date}
                    onChange={(e) => setFormData({ ...formData, record_date: e.target.value })}
                    className={`input-field ${fieldErrors.record_date ? 'border-red-400 ring-1 ring-red-300' : ''}`}
                  />
                  {renderFieldError('record_date')}
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">检测时间</label>
                  <input
                    type="time"
                    value={formData.record_time}
                    onChange={(e) => setFormData({ ...formData, record_time: e.target.value })}
                    className={`input-field ${fieldErrors.record_time ? 'border-red-400 ring-1 ring-red-300' : ''}`}
                  />
                  {renderFieldError('record_time')}
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">设备编号</label>
                  <input
                    type="text"
                    value={formData.device_id}
                    onChange={(e) => setFormData({ ...formData, device_id: e.target.value })}
                    className="input-field"
                    placeholder="sensor-01 / manual"
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-4">
                {METRICS.map((m) => (
                  <div key={m.key}>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      {m.label}{m.unit ? `(${m.unit})` : ''}
                    </label>
                    <input
                      type="number"
                      step={m.step}
                      value={formData[m.key]}
                      onChange={(e) => setFormData({ ...formData, [m.key]: e.target.value })}
                      className={`input-field ${fieldErrors[m.key] ? 'border-red-400 ring-1 ring-red-300' : ''}`}
                      placeholder={`留空=缺测，范围 ${m.min}~${m.max}`}
                    />
                    {renderFieldError(m.key)}
                  </div>
                ))}
              </div>
              <p className="text-xs text-gray-400 -mt-2">
                留空表示该指标本次缺测；输入 0 表示实测零值，二者都会被如实保存与区分。
              </p>

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

              {quarantineOffer && (
                <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
                  <p className="font-medium flex items-center gap-1"><ShieldAlert size={15} /> 数据未通过边界/时间校验</p>
                  <p className="mt-1 text-amber-700">
                    可修正后重新提交，或先隔离保存、由管理人员复核。非有限数等硬性错误不能隔离。
                  </p>
                  <button
                    type="button"
                    onClick={() => submitForm(true)}
                    className="mt-2 text-xs btn-secondary py-1.5 px-3 border-amber-300 text-amber-800"
                  >
                    仍然隔离保存
                  </button>
                </div>
              )}

              <div className="flex justify-end space-x-3 pt-2">
                <button type="button" onClick={() => setShowModal(false)} className="btn-secondary">取消</button>
                <button type="submit" className="btn-primary">
                  {editingRecord ? '保存更正' : '创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 复核弹窗 */}
      {reviewRecord && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-xl mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-gray-900 flex items-center gap-2">
                <ShieldAlert size={20} className="text-amber-500" />
                复核隔离记录 #{reviewRecord.id}
              </h2>
              <button onClick={() => setReviewRecord(null)} className="p-2 text-gray-400 hover:text-gray-600">
                <X size={20} />
              </button>
            </div>

            <div className="text-sm text-gray-600 mb-4 space-y-1">
              <p>批次：{getBatchNumber(reviewRecord.batch_id)} ｜ 采样：{reviewRecord.record_date} {reviewRecord.record_time}</p>
              <p>设备：{reviewRecord.device_id || 'manual'}</p>
              {reviewRecord.notes && <p>备注：{reviewRecord.notes}</p>}
            </div>

            <div className="grid grid-cols-3 gap-3 mb-4">
              {METRICS.map((m) => {
                const flagged = !!reviewRecord.invalid_field_reasons?.[m.key];
                return (
                  <div key={m.key} className={flagged ? 'rounded-lg p-2 bg-red-50 border border-red-200' : 'rounded-lg p-2 bg-gray-50'}>
                    <label className="block text-xs text-gray-600 mb-1">
                      {m.label}{m.unit ? `(${m.unit})` : ''}
                      {flagged && <span className="text-red-600"> 异常</span>}
                    </label>
                    <input
                      type="number"
                      step={m.step}
                      defaultValue={reviewRecord[m.key] ?? ''}
                      placeholder={reviewRecord[m.key] === null || reviewRecord[m.key] === undefined ? '缺测' : ''}
                      onChange={(e) => setCorrections((prev) => ({ ...prev, [m.key]: e.target.value }))}
                      className={`input-field text-sm py-1.5 ${reviewErrors[m.key] ? 'border-red-400 ring-1 ring-red-300' : ''}`}
                    />
                    {reviewErrors[m.key] && (
                      <p className="mt-1 text-xs text-red-600">{reviewErrors[m.key]}</p>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="space-y-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">复核人</label>
                <input
                  type="text"
                  value={reviewer}
                  onChange={(e) => setReviewer(e.target.value)}
                  className="input-field"
                  placeholder="姓名"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  复核结论 <span className="text-red-500">*</span>
                </label>
                <textarea
                  value={reviewConclusion}
                  onChange={(e) => setReviewConclusion(e.target.value)}
                  className="input-field"
                  rows={3}
                  placeholder="说明异常原因（如：化验员误录 / 传感器故障 / 复测值为…），结论随记录长期留痕"
                />
                {reviewErrors.form && <p className="mt-1 text-xs text-red-600">{reviewErrors.form}</p>}
              </div>
            </div>

            <div className="flex justify-end gap-3 pt-4">
              <button onClick={() => setReviewRecord(null)} className="btn-secondary">取消</button>
              <button
                disabled={reviewSaving}
                onClick={() => submitReview('confirm_invalid')}
                className="px-4 py-2 rounded-lg bg-red-100 text-red-700 hover:bg-red-200 text-sm font-medium"
              >
                确认作废（隔离保留）
              </button>
              <button
                disabled={reviewSaving}
                onClick={() => submitReview('confirm_valid')}
                className="btn-primary text-sm"
              >
                更正后确认有效
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

function latestPlanPoint(point: WaterQualityTrendPoint | undefined, key: WaterQualityMetricKey): number | '' {
  const v = point?.[key];
  return v === null || v === undefined ? '' : (v as number);
}

const RevisionHistory: React.FC<{ revisions: WaterQualityRevision[] }> = ({ revisions }) => {
  if (!revisions || revisions.length === 0) {
    return <p className="text-xs text-gray-400 py-2">暂无修订记录</p>;
  }
  return (
    <div className="py-2">
      <p className="text-xs font-medium text-gray-500 mb-2">修订留痕（传感器原值与人工更正均可回看）</p>
      <ol className="space-y-2">
        {revisions.map((rev) => (
          <li key={rev.id} className="text-xs text-gray-600 flex gap-2">
            <span className="shrink-0 px-1.5 py-0.5 rounded bg-white border border-gray-200 text-gray-700">
              {ACTION_LABEL[rev.action] ?? rev.action}
            </span>
            <span className="text-gray-400">{new Date(rev.created_at).toLocaleString('zh-CN')}</span>
            <span>来源：{rev.changed_by || '-'}</span>
            {rev.field_changes && (
              <span className="text-gray-500 font-mono break-all">
                {JSON.stringify(rev.field_changes, null, 0).slice(0, 400)}
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
};

export default WaterQuality;
