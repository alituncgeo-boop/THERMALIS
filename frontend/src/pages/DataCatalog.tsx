import { Layers, Lock, Plus, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { errorKey } from "../api";
import { EmptyState } from "../components/common";
import { PageHeader } from "../components/Layout";
import { RequireProject } from "../components/projects";
import { useApp } from "../state";

interface LayerSummary {
  name: string;
  role: "core" | "auxiliary" | "unclassified";
  published: boolean;
  geometry_type: string;
  feature_count: number;
}

interface VersionSummary {
  dataset_id: string;
  version: string;
  title: { label: string } | null;
  quality_status: string;
  registered_at: string;
  source_owner: string | null;
  access: string | null;
  crs_name: string | null;
  layers: LayerSummary[];
  exception_count: number;
}

interface VersionDetail {
  record: {
    source: { package_id: string; owner: string | null; origin_path: string | null };
    crs: { native_crs_name: string; transformed: boolean };
    documents: string[];
    checks: { id: string; status: string }[];
  };
  quality: {
    status: string;
    summary: Record<string, number>;
    gates: { id: string; status: string; detail: string }[];
    fitness_for_use: string;
  };
  integrity: { file_count: number; total_bytes: number; verifications: Record<string, { status: string }> };
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(String(response.status));
  return (await response.json()) as T;
}

function QualityPill({ status }: { status: string }) {
  const { t } = useTranslation();
  return <span className={`quality-pill q-${status.toLowerCase()}`}>{t(`data.quality.${status}`, { defaultValue: status })}</span>;
}

export function DataPage() {
  const { t, i18n } = useTranslation();
  const project = useApp().activeProject?.name ?? "";
  const [versions, setVersions] = useState<VersionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<VersionSummary | null>(null);
  const [detail, setDetail] = useState<VersionDetail | null>(null);
  const number = (n: number) => n.toLocaleString(i18n.language === "tr" ? "tr-TR" : "en-GB");
  const date = (iso: string) => new Date(iso).toLocaleString(i18n.language === "tr" ? "tr-TR" : "en-GB");

  useEffect(() => {
    getJson<VersionSummary[]>("/api/data/versions")
      .then((list) => {
        setVersions(list);
        if (list.length) setSelected(list[0]);
      })
      .catch((err) => setError(errorKey(err)));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setDetail(null);
    const query = new URLSearchParams({ dataset_id: selected.dataset_id, version: selected.version });
    getJson<VersionDetail>(`/api/data/version?${query}`).then(setDetail).catch((err) => setError(errorKey(err)));
  }, [selected]);

  return (
    <RequireProject>
      <PageHeader
        lead="THERMALIS"
        accent="DATA"
        subtitle={t("pages.data.subtitle", { project })}
        actions={
          <button type="button" className="primary-button" disabled title={t("data.add_via_command")}>
            <Plus size={18} aria-hidden />
            {t("pages.data.add_dataset")}
          </button>
        }
      />
      {error && <p className="form-error">{t(error)}</p>}
      {versions && versions.length === 0 && (
        <div className="panel">
          <EmptyState icon={Layers} accent="var(--c-data)" title={t("pages.data.empty_title")} body={t("data.empty_body")} />
        </div>
      )}
      {versions && versions.length > 0 && (
        <>
          <div className="panel">
            <h2 className="panel-title">{t("data.catalog_title")}</h2>
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t("data.col.dataset")}</th>
                  <th>{t("data.col.version")}</th>
                  <th>{t("data.col.quality")}</th>
                  <th>{t("data.col.layers")}</th>
                  <th>{t("data.col.crs")}</th>
                  <th>{t("data.col.access")}</th>
                  <th>{t("data.col.registered")}</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr
                    key={`${v.dataset_id}@${v.version}`}
                    className={selected === v ? "selected" : ""}
                    onClick={() => setSelected(v)}
                    tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && setSelected(v)}
                  >
                    <td>
                      <div className="cell-strong">{v.title?.label ?? v.dataset_id}</div>
                      <div className="cell-muted mono">{v.dataset_id}</div>
                    </td>
                    <td className="mono">{v.version}</td>
                    <td><QualityPill status={v.quality_status} /></td>
                    <td>
                      {v.layers.filter((l) => l.role === "core").map((l) => (
                        <div key={l.name}>
                          {l.name} <span className="cell-muted">· {number(l.feature_count)}</span>
                        </div>
                      ))}
                    </td>
                    <td className="mono small-cell">{v.crs_name ?? "—"}</td>
                    <td>{v.access === "RESTRICTED" && <span className="badge badge-warn"><Lock size={12} aria-hidden /> {t("data.restricted")}</span>}</td>
                    <td className="small-cell">{date(v.registered_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {selected && (
            <div className="detail-grid">
              <div className="panel">
                <h2 className="panel-title">{t("data.layers_title")}</h2>
                <table className="data-table compact">
                  <thead>
                    <tr>
                      <th>{t("data.col.layer")}</th>
                      <th>{t("data.col.role")}</th>
                      <th>{t("data.col.geometry")}</th>
                      <th className="num">{t("data.col.features")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.layers.map((l) => (
                      <tr key={l.name} className={l.published ? "" : "muted-row"}>
                        <td className="mono">{l.name}</td>
                        <td>{t(`data.role.${l.role}`)}</td>
                        <td>{l.geometry_type}</td>
                        <td className="num">{number(l.feature_count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {detail && (
                  <ul className="fact-list">
                    <li>
                      <ShieldCheck size={16} aria-hidden /> {t("data.integrity", { files: number(detail.integrity.file_count) })}{" "}
                      {Object.values(detail.integrity.verifications).map((v) => v.status).join(" · ")}
                    </li>
                    <li>{t("data.crs_kept", { name: detail.record.crs.native_crs_name })}</li>
                    <li>{t("data.documents", { count: detail.record.documents.length })}</li>
                    <li>{t("data.source", { owner: detail.record.source.owner ?? "—", pkg: detail.record.source.package_id })}</li>
                  </ul>
                )}
              </div>
              <div className="panel">
                <h2 className="panel-title">
                  {t("data.quality_title")} <QualityPill status={selected.quality_status} />
                </h2>
                {detail && (
                  <>
                    <p className="muted small-gap">{t("data.quality_note")}</p>
                    <table className="data-table compact">
                      <tbody>
                        {Object.entries(detail.quality.summary).map(([category, count]) => (
                          <tr key={category}>
                            <td>{t(`data.category.${category}`, { defaultValue: category })}</td>
                            <td className="num">{number(count)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {detail.quality.gates.length > 0 && (
                      <div className="gates">
                        {detail.quality.gates.map((g) => (
                          <span key={g.id} className={`gate gate-${g.status.toLowerCase()}`} title={g.detail}>
                            {g.id} · {g.status}
                          </span>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </RequireProject>
  );
}
