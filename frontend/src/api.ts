import type { Language } from "./i18n";

export interface Settings {
  language: Language;
  data_root: string;
  data_root_is_default: boolean;
  active_project_id: string | null;
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
  updateSettings: (update: Partial<Pick<Settings, "language" | "data_root" | "active_project_id">>) =>
    request<Settings>("/settings", { method: "PUT", body: JSON.stringify(update) }),
  projects: () => request<Project[]>("/projects"),
  createProject: (name: string, description: string) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify({ name, description }) }),
};

const KNOWN_ERRORS = new Set([
  "project_name_exists",
  "project_not_found",
  "data_root_not_absolute",
  "data_root_not_writable",
  "validation",
  "network",
]);

export function errorKey(error: unknown): string {
  const code = error instanceof ApiError ? error.code : "unknown";
  return `errors.${KNOWN_ERRORS.has(code) ? code : "unknown"}`;
}
