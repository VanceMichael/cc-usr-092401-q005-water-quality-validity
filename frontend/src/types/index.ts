export interface Pond {
  id: number;
  name: string;
  area: number;
  water_depth: number;
  species?: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface Batch {
  id: number;
  batch_number: string;
  pond_id: number;
  species: string;
  stocking_date: string;
  estimated_harvest_date?: string;
  actual_harvest_date?: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface StockingRecord {
  id: number;
  batch_id: number;
  species: string;
  quantity: number;
  source?: string;
  batch_number?: string;
  weight_per_unit?: number;
  total_weight?: number;
  notes?: string;
  created_at: string;
}

export interface FeedingRecord {
  id: number;
  batch_id: number;
  feeding_date: string;
  feed_type: string;
  feed_quantity: number;
  feeding_time?: string;
  weather?: string;
  water_temperature?: number;
  notes?: string;
  created_at: string;
}

export type WaterQualityStatus = 'valid' | 'quarantined' | 'rejected';

export interface WaterQualityRevision {
  id: number;
  action: string;
  changed_by?: string;
  field_changes?: Record<string, unknown>;
  created_at: string;
}

export interface WaterQualityRecord {
  id: number;
  batch_id: number;
  record_date: string;
  record_time?: string;
  sampled_at?: string;
  device_id?: string;
  water_temperature?: number | null;
  ph_value?: number | null;
  dissolved_oxygen?: number | null;
  ammonia_nitrogen?: number | null;
  nitrite?: number | null;
  transparency?: number | null;
  notes?: string;
  status: WaterQualityStatus;
  invalid_field_reasons?: Record<string, string> | null;
  review_conclusion?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  merged_into_id?: number | null;
  revisions?: WaterQualityRevision[];
  merged_from_duplicate?: boolean;
  created_at: string;
  updated_at?: string;
}

export interface WaterQualityTrendPoint {
  record_id: number;
  sampled_at: string;
  record_date: string;
  record_time?: string;
  device_id?: string;
  water_temperature?: number | null;
  ph_value?: number | null;
  dissolved_oxygen?: number | null;
  ammonia_nitrogen?: number | null;
  nitrite?: number | null;
  transparency?: number | null;
}

export interface WaterQualityTrend {
  batch_id?: number | null;
  points: WaterQualityTrendPoint[];
  quarantined_count: number;
  rejected_count: number;
  merged_count: number;
}

export interface WaterQualityReviewPayload {
  action: 'confirm_valid' | 'confirm_invalid';
  conclusion: string;
  reviewed_by?: string;
  corrections?: Partial<Record<WaterQualityMetricKey, number | null>>;
}

export type WaterQualityMetricKey =
  | 'water_temperature'
  | 'ph_value'
  | 'dissolved_oxygen'
  | 'ammonia_nitrogen'
  | 'nitrite'
  | 'transparency';

export interface FieldErrors {
  [field: string]: string | undefined;
}

export interface MedicationRecord {
  id: number;
  batch_id: number;
  medication_date: string;
  drug_name: string;
  drug_type?: string;
  dosage?: number;
  dosage_unit: string;
  administration_method?: string;
  purpose?: string;
  manufacturer?: string;
  batch_number?: string;
  notes?: string;
  created_at: string;
}

export interface CostRecord {
  id: number;
  batch_id: number;
  cost_date: string;
  cost_type: string;
  amount: number;
  description?: string;
  quantity?: number;
  unit?: string;
  unit_price?: number;
  notes?: string;
  created_at: string;
}

export interface HarvestSale {
  id: number;
  batch_id: number;
  sale_date: string;
  weight: number;
  unit_price: number;
  total_amount?: number;
  buyer?: string;
  batch_number?: string;
  quality_grade?: string;
  notes?: string;
  created_at: string;
}

export interface CostSummary {
  feed_cost: number;
  medicine_cost: number;
  labor_cost: number;
  electricity_cost: number;
  other_cost: number;
  total_cost: number;
}

export interface FeedingSummary {
  total_feed_weight: number;
  feeding_count: number;
  avg_daily_feed: number;
}

export interface CultureCycleAnalysis {
  batch_number: string;
  pond_name: string;
  species: string;
  stocking_date: string;
  harvest_date?: string;
  days_cultured?: number;
  initial_quantity: number;
  harvest_weight: number;
  survival_rate: number;
  feed_total: number;
  feed_conversion_ratio: number;
  area: number;
  yield_per_mu: number;
  total_cost: number;
  total_revenue: number;
  profit: number;
  cost_summary?: CostSummary;
  feeding_summary?: FeedingSummary;
}

export interface ApiResponse<T> {
  data?: T;
  message?: string;
}

export interface StockingRecordTrace {
  species: string;
  quantity: number;
  source?: string;
  batch_number?: string;
  stocking_date?: string;
}

export interface FeedingRecordTrace {
  feeding_date: string;
  feed_type: string;
  quantity: number;
  unit?: string;
}

export interface WaterQualityRecordTrace {
  record_date: string;
  water_temperature?: number;
  ph_value?: number;
  dissolved_oxygen?: number;
}

export interface MedicationRecordTrace {
  medication_date: string;
  medication_name: string;
  dosage?: number;
  unit?: string;
}

export interface CostRecordTrace {
  cost_date: string;
  cost_type: string;
  amount: number;
  description?: string;
}

export interface HarvestSaleTrace {
  sale_date: string;
  weight: number;
  unit_price: number;
  total_amount?: number;
  buyer?: string;
}

export interface BatchInfo {
  batch_number: string;
  species: string;
  stocking_date: string;
  harvest_date?: string;
  status: string;
  pond_id?: number;
}

export interface PondInfo {
  name?: string;
  area?: number;
  water_depth?: number;
}

export interface BatchTraceability {
  batch: BatchInfo;
  pond_info: PondInfo;
  stocking_records: StockingRecordTrace[];
  feeding_records: FeedingRecordTrace[];
  water_quality_records: WaterQualityRecordTrace[];
  medication_records: MedicationRecordTrace[];
  cost_records: CostRecordTrace[];
  harvest_sales: HarvestSaleTrace[];
}
