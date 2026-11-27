import type { Language } from "./i18n";

export interface Settings {
  language: Language;
  data_root: string;
  data_root_is_default: boolean;
  active_project_id: string | null;
  transfer_incoming_dir: string | null;
  transfer_watch_enabled: boolean;
  transfer_scan_interval_seconds: number;
  transfer_settle_seconds: number;
}

/* ---------- DATA kataloğu ---------- */
export interface Extent {
  minx: number;
  miny: number;
  maxx: number;
  maxy: number;
  crs: string | null;
}

export interface CatalogItem {
  id: string;
  dataset_id: string;
  version: string;
  package_id: string | null;
  package_version: string | null;
  package_fingerprint: string | null;
  layer_name: string;
  place: string | null;
  source_title: string | null;
  category: string | null;
  source_institution: string | null;
  format: string | null;
  format_key: string | null;
  asset_id: string | null;
  asset_path: string | null;
  geometry_type: string | null;
  feature_count: number | null;
  role: string;
  crs_name: string | null;
  spatial_extent: Extent | null;
  temporal: {
    state: string; currency_year: string | null; valid_from: string | null; valid_to: string | null;
    /** Anlamlı zaman girdileri (tür + değer + süreklilik + durum). Kayıt tarihi kaynak tarihi yerine konmaz. */
    entries?: TemporalEntry[];
  };
  quality_status: string;
  access_class: string;
  integrity: { status: string | null; checked_at: string | null; last_successful_at: string | null };
  metadata_completeness: { total: number; present: number; partial: number; missing: number; unknown: number; not_applicable: number };
  registered_at: string | null;
  summary: string | null;
  description: string | null;
  record_issue_count: number;
  version_count: number;
  /** Kayıtlı sürümün depolama yeri; bulut kayıtları yalnız commit işareti doğrulanmışsa görünür. */
  storage?: StorageLocation;
}

export interface StorageLocation {
  backend: "local" | "r2";
  provider: string | null;
  bucket: string | null;
  committed_at: string | null;
  total_bytes: number | null;
  synced_at: string | null;
  /** Geçiş döneminde yerel kaydın doğrulanmış bulut kopyası (katalog yerel kaydı kullanmaya devam eder). */
  cloud_copy?: { bucket: string | null; committed_at: string | null; same_fingerprint: boolean } | null;
  /** Sürüm başına yetkili kaynak ve (varsa) ikincil kopyanın deposu. */
  authoritative?: "local" | "r2";
  secondary_copy?: "local" | "r2" | null;
}

export interface TemporalEntry {
  kind: string;
  start_value: string | null;
  end_value: string | null;
  continuity: string;
  status: string;
  scope: string;
  basis?: string | null;
  note?: string | null;
}

export interface VerticalReference {
  status: string;
  reason?: string | null;
  datum_name?: string | null;
  epsg?: number | null;
  height_type?: string | null;
  unit?: string | null;
  surface_model?: string | null;
  accuracy_status?: string | null;
  evidence?: { source: string; detail?: string }[];
}

export interface DataAvailability {
  reference_geography?: string | null;
  missing_semantics?: string | null;
  zero_semantics?: string | null;
  coverage?: Record<string, number | string | null> | null;
  limitation?: string | null;
}

export interface MetadataRevision {
  revision_id: string;
  created_at: string | null;
  created_by: string | null;
  reason: string | null;
  paths: string[];
  applied: boolean;
}

export interface StorageStatus {
  active_backend: string;
  cloud: {
    provider: string;
    configured: boolean;
    bucket: string | null;
    intake_bucket: string | null;
    separate_intake_bucket: boolean;
    credentials: { uploader: boolean | null; publisher: boolean | null };
    intake_enabled: boolean;
    connection: string | null;
    last_scan_finished: string | null;
    last_error: string | null;
    interval_seconds: number;
  };
  catalog: {
    cloud_release_count: number;
    local_release_count: number;
    /** Katalogdan türetilmiş (commit işaretlerindeki nesne boyutlarının toplamı); sağlayıcı fatura ölçümü değildir. */
    committed_bytes: number | null;
    committed_bytes_source: string;
    last_sync_success_at: string | null;
    last_sync_result: string | null;
    last_sync_problems: string[];
  };
  cache: { max_bytes: number; usage_bytes: number | null; status?: string };
  staging: { usage_bytes: number; max_bytes: number };
  billing_note: string;
}

export interface Finding {
  seq: number;
  exception_id: string;
  id_unique: number;
  origin: string;
  kind: string;
  category: string;
  subject: string | null;
  feature_a: string | null;
  oid_a: number | null;
  feature_b: string | null;
  oid_b: number | null;
  area_m2: number | null;
  percent_of_feature: number | null;
  evidence: string | null;
  disposition: string | null;
  source_file: string | null;
}

type Row = Record<string, string | number | null>;

export interface DatasetMetadata {
  item: CatalogItem;
  identity: Row;
  version_history: Row[];
  source: Row & { profile: Row | null; layer_metadata: Row | null; copy_verification: unknown };
  assets: Row[];
  crs: Row | null;
  geometry: Row & { extent: Extent | null };
  fields: Row[];
  dictionary: Row[];
  domains: {
    name: string; description: string | null; values: { code: string; label: string }[];
    domain_kind?: string | null; min_value?: string | null; max_value?: string | null; origin?: string | null;
    semantic_status?: string | null; unexpected_codes?: string | null;
  }[];
  temporal_entries?: TemporalEntry[];
  scientific?: { vertical_reference: VerticalReference | null; data_availability: DataAvailability | null };
  metadata_revisions?: MetadataRevision[];
  copies?: { backend: string; role: string; same_fingerprint: boolean; note: string | null }[];
  quality: {
    status: string;
    status_source: string | null;
    upgrade_policy: string | null;
    steward_disposition: string | null;
    fitness_for_use: string | null;
    gates: Row[];
    findings: Finding[];
    version_summary: { records: number; unique_exception_ids: number; duplicated_exception_ids: string[]; by_origin: Record<string, number>; by_kind: Record<string, number> };
    layer_status: Row[];
  };
  access: Record<string, string | number | boolean | null>;
  related_datasets: { id: string; layer_name: string; role: string; published: boolean; feature_count: number; shares_asset: boolean }[];
  integrity: Row & { verifications: Record<string, string>; history: Row[] };
  record_issues: Row[];
  metadata_completeness: { item: string; state: string; detail: string }[];
  registration_checks: Row[];
}

export interface SemanticField {
  layer_dataset_id: string;
  version: string;
  field: string;
  data_type: string | null;
  source_type: string | null;
  alias: string | null;
  meaning: string | null;
  description: string | null;
  status: "SOURCE_DECLARED" | "INFERRED_UNVERIFIED" | "USER_VERIFIED" | "UNKNOWN";
  origin: string | null;
  source_ref: string | null;
  unit: string | null;
  unit_status: string | null;
  unit_origin: string | null;
  domain_name: string | null;
  null_meaning: string | null;
  null_meaning_status: string | null;
  null_count: number | null;
  verified_at: string | null;
  /** Doğrulayanın beyan ettiği ad; kimlik doğrulaması değildir. */
  verified_by: string | null;
  /** İsteğin geldiği yetkili yerel kanal (ui_session / component_token / in_process). */
  verification_auth: string | null;
  field_role: "structural" | "geometry_measure" | "attribute" | null;
  proposal_compatibility: string | null;
}

export interface VerificationLogIntegrity {
  status: "ok" | "broken" | "empty";
  entries: number;
  valid: number;
  broken_at_line: number | null;
  reason: string | null;
}

export interface MetadataConflict {
  kind: string;
  subject: string | null;
  item: string | null;
  authoritative_value: string | null;
  authoritative_source: string | null;
  other_value: string | null;
  other_source: string | null;
  issue: string;
}

export interface SemanticDictionary {
  fields: SemanticField[];
  content_id: string | null;
  verification_log: VerificationLogIntegrity | null;
  conflicts: MetadataConflict[];
}

export interface SessionStatus {
  verification_allowed: boolean;
  channel: string | null;
  identity_verified: boolean;
}

export interface SemanticVerification {
  dataset_id: string;
  version: string;
  field: string;
  meaning?: string;
  description?: string;
  unit?: string;
  null_meaning?: string;
  note?: string;
  verified_by?: string;
}

export interface IntakePackage {
  name: string;
  path: string;
  state: string;
  reason: string;
  reason_code?: string;
  dataset_id?: string | null;
  version?: string | null;
  updated_at?: string;
  state_since?: string;
  present?: boolean;
  technical?: string;
  quality?: string;
  item_id?: string;
  result_path?: string | null;
  support_gap_path?: string | null;
  user_decision?: string | null;
  source?: string;
  /** Bulut kabulü: indirme/doğrulama ilerlemesi ve sonuç. */
  bytes_done?: number;
  bytes_total?: number;
  cloud_state?: "ACCEPTED" | "REJECTED";
  storage?: string;
  release_prefix?: string;
}

export interface IntakeStatus {
  watcher: {
    enabled: boolean;
    running: boolean;
    incoming: string | null;
    interval_seconds: number | null;
    settle_seconds: number | null;
    last_scan_finished: string | null;
    last_result: string | null;
    last_error: string | null;
  };
  packages: IntakePackage[];
  counts: Record<string, number>;
  recent_runs: Row[];
}

export interface Project {
  schema_version: string;
  id: string;
  name: string;
  description: string;
  created_at: string;
}

export interface Health {
  status: string;
  version: string;
}

/** Sunucu hatası; `code` doğrudan `errors.<code>` çeviri anahtarına karşılık gelir. */
export class ApiError extends Error {
  constructor(public code: string) {
    super(code);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError("network");
  }
  if (!response.ok) {
    let code = response.status === 422 ? "validation" : "unknown";
    try {
      const body = await response.json();
      if (typeof body?.detail?.code === "string") code = body.detail.code;
    } catch {
      /* gövde JSON değil */
    }
    throw new ApiError(code);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),
  settings: () => request<Settings>("/settings"),
  updateSettings: (update: Partial<Omit<Settings, "data_root_is_default">>) =>
    request<Settings>("/settings", { method: "PUT", body: JSON.stringify(update) }),
  projects: () => request<Project[]>("/projects"),
  createProject: (name: string, description: string) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify({ name, description }) }),
  catalog: () => request<{ datasets: CatalogItem[] }>("/data/catalog"),
  dataset: (id: string, version?: string) =>
    request<DatasetMetadata>(`/data/dataset?${new URLSearchParams(version ? { id, version } : { id })}`),
  intake: () => request<IntakeStatus>("/data/intake"),
  scanNow: () => request<unknown>("/data/intake/scan", { method: "POST" }),
  storage: () => request<StorageStatus>("/data/storage"),
  storageSync: () => request<unknown>("/data/storage/sync", { method: "POST" }),
  submitPackage: (path: string, expect_dataset_id?: string) =>
    request<{ accepted: boolean }>("/data/intake/submit", { method: "POST", body: JSON.stringify({ path, expect_dataset_id }) }),
  retryPackage: (key: string) => request<unknown>("/data/intake/retry", { method: "POST", body: JSON.stringify({ key }) }),
  verify: (dataset_id: string, version: string) =>
    request<{ status: string }>("/data/verify", { method: "POST", body: JSON.stringify({ dataset_id, version }) }),
  exportUrl: (format: "json" | "csv") => `/api/data/export?format=${format}`,
  semantics: (id: string, version: string) =>
    request<SemanticDictionary>(`/data/v1/semantics?${new URLSearchParams({ id, version })}`),
  session: () => request<SessionStatus>("/session"),
  claimSession: (code: string) => request<SessionStatus>("/session/claim", { method: "POST", body: JSON.stringify({ code }) }),
  verifySemantic: (body: SemanticVerification) =>
    request<SemanticField>("/data/v1/semantics/verify", { method: "POST", body: JSON.stringify(body) }),
};

const KNOWN_ERRORS = new Set([
  "project_name_exists",
  "project_not_found",
  "data_root_not_absolute",
  "data_root_not_writable",
  "path_not_absolute",
  "path_not_found",
  "path_is_link",
  "package_invalid",
  "package_dataset_mismatch",
  "dataset_not_found",
  "intake_item_not_retryable",
  "field_not_found",
  "version_not_found",
  "verification_not_authorized",
  "verification_version_mismatch",
  "verification_log_broken",
  "verification_log_busy",
  "session_code_invalid",
  "cloud_unavailable",
  "physical_access_not_authorized",
  "cache_capacity_exceeded",
  "insufficient_disk_space",
  "validation",
  "network",
]);

export function errorKey(error: unknown): string {
  const code = error instanceof ApiError ? error.code : "unknown";
  return `errors.${KNOWN_ERRORS.has(code) ? code : "unknown"}`;
}
