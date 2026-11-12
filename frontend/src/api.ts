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
  temporal: { state: string; currency_year: string | null; valid_from: string | null; valid_to: string | null };
  quality_status: string;
  access_class: string;
  integrity: { status: string | null; checked_at: string | null; last_successful_at: string | null };
  metadata_completeness: { total: number; present: number; partial: number; missing: number; unknown: number; not_applicable: number };
  registered_at: string | null;
  summary: string | null;
  description: string | null;
  record_issue_count: number;
  version_count: number;
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
  domains: { name: string; description: string | null; values: { code: string; label: string }[] }[];
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
  submitPackage: (path: string, expect_dataset_id?: string) =>
    request<{ accepted: boolean }>("/data/intake/submit", { method: "POST", body: JSON.stringify({ path, expect_dataset_id }) }),
  retryPackage: (key: string) => request<unknown>("/data/intake/retry", { method: "POST", body: JSON.stringify({ key }) }),
  verify: (dataset_id: string, version: string) =>
    request<{ status: string }>("/data/verify", { method: "POST", body: JSON.stringify({ dataset_id, version }) }),
  exportUrl: (format: "json" | "csv") => `/api/data/export?format=${format}`,
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
  "validation",
  "network",
]);

export function errorKey(error: unknown): string {
  const code = error instanceof ApiError ? error.code : "unknown";
  return `errors.${KNOWN_ERRORS.has(code) ? code : "unknown"}`;
}
