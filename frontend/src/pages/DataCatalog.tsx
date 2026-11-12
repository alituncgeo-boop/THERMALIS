import { AlertTriangle, Download, FileSearch, FolderInput, Layers, Lock, Plus, RefreshCw, Search, ShieldCheck, Upload, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { api, errorKey, type CatalogItem, type IntakePackage, type IntakeStatus } from "../api";
import { EmptyState } from "../components/common";
import { PageHeader } from "../components/Layout";
import { RequireProject } from "../components/projects";
import { useApp } from "../state";
import { categories, filterCatalog, intakeBuckets, isMissing, latestRegistered } from "./dataModel";

const POLL_MS = 5000;
const RETRYABLE = ["BLOCKED", "ADAPTER_REQUIRED", "FAILED"];

/* ---------- ortak biçimlendirme ---------- */
export function useDataFormat() {
  const { t, i18n } = useTranslation();
  const locale = i18n.language === "tr" ? "tr-TR" : "en-GB";
  return useMemo(() => {
    const unknown = t("data.value.unknown");
    const notProvided = t("data.value.not_provided");
    const number = (n: number | null | undefined, digits = 0) =>
      isMissing(n) ? unknown : (n as number).toLocaleString(locale, { maximumFractionDigits: digits });
    const date = (iso: string | null | undefined) => (isMissing(iso) ? unknown : new Date(iso as string).toLocaleString(locale));
    const text = (v: unknown, fallback = notProvided) => (isMissing(v) ? fallback : String(v));
    const title = (item: Pick<CatalogItem, "place" | "layer_name">) => {
      const layer = t(`data.layer_name.${item.layer_name}`, { defaultValue: item.layer_name });
      return item.place ? t("data.title_pattern", { place: item.place, layer }) : layer;
    };
    const category = (c: string | null) => (c ? t(`data.dataset_category.${c}`, { defaultValue: c }) : notProvided);
    const temporal = (tc: CatalogItem["temporal"]) => {
      if (tc.state === "unknown" || !tc.currency_year) return notProvided;
      const range = tc.valid_from && tc.valid_to ? `${tc.valid_from} – ${tc.valid_to}` : t("data.temporal.range_not_provided");
      return t("data.temporal.currency", { year: tc.currency_year, range });
    };
    const extent = (e: CatalogItem["spatial_extent"]) =>
      e ? t("data.extent", { minx: number(e.minx), maxx: number(e.maxx), miny: number(e.miny), maxy: number(e.maxy), crs: e.crs ?? unknown }) : unknown;
    return { t, unknown, notProvided, number, date, text, title, category, temporal, extent };
  }, [t, locale]);
}

export function QualityPill({ status }: { status: string }) {
  const { t } = useTranslation();
  return <span className={`quality-pill q-${status.toLowerCase()}`}>{t(`data.quality.${status}`, { defaultValue: status })}</span>;
}

export function AccessBadge({ access }: { access: string | null }) {
  const { t } = useTranslation();
  if (access === "RESTRICTED") return <span className="badge badge-warn"><Lock size={12} aria-hidden /> {t("data.access.RESTRICTED")}</span>;
  return <span className="badge">{t(`data.access.${access ?? "UNKNOWN"}`, { defaultValue: access ?? "" })}</span>;
}

function StateBadge({ state }: { state: string }) {
  const { t } = useTranslation();
  return <span className={`state-badge st-${state.toLowerCase()}`}>{t(`data.intake.state.${state}`, { defaultValue: state })}</span>;
}

/* ---------- işlem durumu ---------- */
function IntakePanel({ intake, items, onScan, onRetry }: { intake: IntakeStatus | null; items: CatalogItem[]; onScan: () => void; onRetry: (p: IntakePackage) => void }) {
  const f = useDataFormat();
  const { t } = f;
  const [open, setOpen] = useState(false);
  const buckets = intakeBuckets(intake?.counts ?? {});
  const latest = latestRegistered(items);
  const w = intake?.watcher;
  const visible = (intake?.packages ?? []).filter((p) => p.present !== false);
  const attention = visible.filter((p) => !["REGISTERED", "ALREADY_REGISTERED"].includes(p.state));

  return (
    <div className="panel intake-panel">
      <div className="intake-head">
        <h2 className="panel-title">{t("data.intake.title")}</h2>
        <div className="intake-watch small-cell">
          <FolderInput size={16} aria-hidden />
          {!w ? f.unknown
            : !w.enabled ? t("data.intake.watch_off")
            : t("data.intake.watching", { folder: w.incoming, seconds: w.interval_seconds })}
          {w?.last_scan_finished && <span className="cell-muted"> · {t("data.intake.last_scan", { at: f.date(w.last_scan_finished) })}</span>}
          {w?.last_result && w.last_result !== "ok" && <span className="form-error"> · {t(`data.intake.result.${w.last_result}`, { defaultValue: w.last_result })}{w.last_error ? `: ${w.last_error}` : ""}</span>}
          <button type="button" className="ghost-button small-button" onClick={onScan} disabled={!w?.enabled}>
            <RefreshCw size={14} aria-hidden /> {t("data.intake.scan_now")}
          </button>
        </div>
      </div>
      <div className="stat-grid">
        <div className="stat-card"><span className="stat-label">{t("data.intake.stat.registered")}</span><span className="stat-value">{f.number(items.length)}</span></div>
        <div className="stat-card wide">
          <span className="stat-label">{t("data.intake.stat.latest")}</span>
          <span className="stat-text">{latest ? `${f.title(latest)} · ${f.date(latest.registered_at)}` : f.notProvided}</span>
        </div>
        <div className="stat-card"><span className="stat-label">{t("data.intake.stat.in_progress")}</span><span className="stat-value">{f.number(buckets.inProgress)}</span></div>
        <div className="stat-card"><span className="stat-label">{t("data.intake.stat.waiting")}</span><span className="stat-value">{f.number(buckets.waiting)}</span></div>
        <div className={`stat-card ${buckets.adapterRequired ? "warn" : ""}`}><span className="stat-label">{t("data.intake.stat.adapter_required")}</span><span className="stat-value">{f.number(buckets.adapterRequired)}</span></div>
        <div className={`stat-card ${buckets.failed ? "bad" : ""}`}><span className="stat-label">{t("data.intake.stat.failed")}</span><span className="stat-value">{f.number(buckets.failed)}</span></div>
      </div>
      {visible.length > 0 && (
        <button type="button" className="ghost-button small-button" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? t("data.intake.hide_packages") : t("data.intake.show_packages", { count: visible.length, attention: attention.length })}
        </button>
      )}
      {open && (
        <table className="data-table compact">
          <thead>
            <tr>
              <th>{t("data.intake.col.package")}</th>
              <th>{t("data.intake.col.state")}</th>
              <th>{t("data.intake.col.dataset")}</th>
              <th>{t("data.intake.col.detail")}</th>
              <th>{t("data.intake.col.updated")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {visible.map((p) => (
              <tr key={p.path}>
                <td className="mono small-cell">{p.name}{p.source === "manual" && <span className="cell-muted"> · {t("data.intake.manual")}</span>}</td>
                <td><StateBadge state={p.state} /></td>
                <td className="mono small-cell">{p.dataset_id ? `${p.dataset_id} ${p.version ?? ""}` : f.unknown}</td>
                <td className="small-cell">
                  {p.reason || "—"}
                  {p.user_decision && <div className="cell-muted">{p.user_decision}</div>}
                  {p.support_gap_path && <div className="cell-muted mono">{p.support_gap_path}</div>}
                </td>
                <td className="small-cell">{f.date(p.updated_at)}</td>
                <td>{RETRYABLE.includes(p.state) && <button type="button" className="ghost-button small-button" onClick={() => onRetry(p)}>{t("data.intake.retry")}</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/* ---------- sağ panel ---------- */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function DetailPanel({ item, onVerified }: { item: CatalogItem; onVerified: () => void }) {
  const f = useDataFormat();
  const { t } = f;
  const navigate = useNavigate();
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mc = item.metadata_completeness;

  async function verify() {
    setVerifying(true);
    setError(null);
    try {
      await api.verify(item.dataset_id, item.version);
      onVerified();
    } catch (err) {
      setError(errorKey(err));
    } finally {
      setVerifying(false);
    }
  }

  return (
    <aside className="panel detail-panel">
      <h2 className="panel-title">{f.title(item)}</h2>
      <p className="cell-muted mono">{item.id}</p>
      {item.record_issue_count > 0 && (
        <p className="notice-warn"><AlertTriangle size={14} aria-hidden /> {t("data.detail.record_issues", { count: item.record_issue_count })}</p>
      )}
      <dl className="kv">
        <Fact label={t("data.detail.dataset_id")}><span className="mono">{item.id}</span></Fact>
        <Fact label={t("data.detail.dataset_version")}><span className="mono">{item.version}</span>{item.version_count > 1 && <span className="cell-muted"> · {t("data.detail.versions", { count: item.version_count })}</span>}</Fact>
        <Fact label={t("data.detail.package_version")}><span className="mono">{f.text(item.package_version, f.unknown)}</span><div className="cell-muted mono">{f.text(item.package_id, f.unknown)}</div></Fact>
        <Fact label={t("data.detail.category")}>{f.category(item.category)}</Fact>
        <Fact label={t("data.detail.source_institution")}>{f.text(item.source_institution)}</Fact>
        <Fact label={t("data.detail.format")}>{item.format_key ? t(`data.format.${item.format_key}`, { defaultValue: item.format ?? item.format_key }) : f.unknown} · {f.text(item.geometry_type, f.unknown)} · {t("data.detail.features", { n: f.number(item.feature_count) })}</Fact>
        <Fact label={t("data.detail.crs")}><span className="mono">{f.text(item.crs_name, f.unknown)}</span></Fact>
        <Fact label={t("data.detail.spatial")}>{f.extent(item.spatial_extent)}</Fact>
        <Fact label={t("data.detail.temporal")}>{f.temporal(item.temporal)}</Fact>
        <Fact label={t("data.detail.quality")}><QualityPill status={item.quality_status} /></Fact>
        <Fact label={t("data.detail.access")}><AccessBadge access={item.access_class} /></Fact>
        <Fact label={t("data.detail.integrity")}>
          {item.integrity.status ? <span className={`gate gate-${item.integrity.status === "PASS" ? "pass" : "hold"}`}>{item.integrity.status}</span> : f.unknown}
          <span className="cell-muted"> · {t("data.detail.last_success", { at: f.date(item.integrity.last_successful_at) })}</span>
        </Fact>
        <Fact label={t("data.detail.completeness")}>{t("data.detail.completeness_value", { present: mc.present, total: mc.total, partial: mc.partial, missing: mc.missing + mc.unknown })}</Fact>
        <Fact label={t("data.detail.registered")}>{f.date(item.registered_at)}</Fact>
        <Fact label={t("data.detail.description")}>{f.text(item.description ?? item.summary)}</Fact>
      </dl>
      {error && <p className="form-error">{t(error)}</p>}
      <div className="detail-actions">
        <button type="button" className="primary-button" onClick={() => navigate(`/data/dataset?${new URLSearchParams({ id: item.id, version: item.version })}`)}>
          <FileSearch size={18} aria-hidden /> {t("data.detail.view_metadata")}
        </button>
        <button type="button" className="ghost-button" onClick={() => void verify()} disabled={verifying}>
          <ShieldCheck size={18} aria-hidden /> {verifying ? t("data.detail.verifying") : t("data.detail.verify")}
        </button>
      </div>
    </aside>
  );
}

/* ---------- elle ekleme / yeni sürüm ---------- */
function PackageDialog({ mode, target, onClose, onSubmitted }: { mode: "add" | "version"; target: CatalogItem | null; onClose: () => void; onSubmitted: () => void }) {
  const { t } = useTranslation();
  const f = useDataFormat();
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.submitPackage(path.trim(), mode === "version" ? target?.dataset_id : undefined);
      onSubmitted();
    } catch (err) {
      setError(errorKey(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="package-dialog-title" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="dialog-header">
          <h2 id="package-dialog-title">{mode === "add" ? t("data.add.title") : t("data.add.version_title")}</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label={t("common.cancel")}><X size={16} /></button>
        </div>
        {mode === "version" && target && <p className="muted small-gap">{t("data.add.version_for", { title: f.title(target), id: target.dataset_id, version: target.version })}</p>}
        <p className="muted small-gap">{t("data.add.help")}</p>
        <label className="field">
          {t("data.add.path_label")}
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder={t("data.add.path_placeholder")} spellCheck={false} autoFocus />
        </label>
        <p className="muted small">{t("data.add.drop_hint")}</p>
        {error && <p className="form-error">{t(error)}</p>}
        <div className="dialog-actions">
          <button type="button" className="ghost-button" onClick={onClose}>{t("common.cancel")}</button>
          <button type="submit" className="primary-button" disabled={!path.trim() || busy}>{t("data.add.submit")}</button>
        </div>
      </form>
    </div>
  );
}

/* ---------- sayfa ---------- */
export function DataPage() {
  const f = useDataFormat();
  const { t } = f;
  const project = useApp().activeProject?.name ?? "";
  const [items, setItems] = useState<CatalogItem[] | null>(null);
  const [intake, setIntake] = useState<IntakeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [dialog, setDialog] = useState<"add" | "version" | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const registeredSignature = useRef<string>("");

  const loadCatalog = useCallback(() => {
    api.catalog()
      .then(({ datasets }) => {
        setItems(datasets);
        setSelectedId((current) => (current && datasets.some((d) => d.id === current) ? current : datasets[0]?.id ?? null));
      })
      .catch((err) => setError(errorKey(err)));
  }, []);

  const loadIntake = useCallback(() => {
    api.intake()
      .then((status) => {
        setIntake(status);
        // Yeni bir kayıt tamamlandıysa katalog yeniden okunur (sayılar daima gerçek katalogdan).
        const signature = status.packages.filter((p) => p.state === "REGISTERED").map((p) => `${p.path}@${p.updated_at}`).join("|");
        if (signature !== registeredSignature.current) {
          registeredSignature.current = signature;
          loadCatalog();
        }
      })
      .catch(() => setIntake(null));
  }, [loadCatalog]);

  useEffect(() => {
    loadCatalog();
    loadIntake();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") loadIntake();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadCatalog, loadIntake]);

  const filtered = useMemo(
    () => filterCatalog(items ?? [], query, category, (i) => [f.title(i), f.category(i.category)]),
    [items, query, category, f],
  );
  const selected = filtered.find((i) => i.id === selectedId) ?? filtered[0] ?? null;

  return (
    <RequireProject>
      <PageHeader
        lead="THERMALIS"
        accent="DATA"
        subtitle={t("pages.data.subtitle", { project })}
        actions={
          <>
            <button type="button" className="ghost-button" disabled={!selected} onClick={() => setDialog("version")} title={selected ? undefined : t("data.add.select_first")}>
              <Upload size={18} aria-hidden /> {t("data.actions.upload_version")}
            </button>
            <div className="menu-anchor">
              <button type="button" className="ghost-button" aria-expanded={exportOpen} onClick={() => setExportOpen(!exportOpen)} disabled={!items?.length}>
                <Download size={18} aria-hidden /> {t("data.actions.export")}
              </button>
              {exportOpen && (
                <div className="menu" role="menu">
                  <a role="menuitem" href={api.exportUrl("json")} download onClick={() => setExportOpen(false)}>JSON</a>
                  <a role="menuitem" href={api.exportUrl("csv")} download onClick={() => setExportOpen(false)}>CSV</a>
                </div>
              )}
            </div>
            <button type="button" className="primary-button" onClick={() => setDialog("add")}>
              <Plus size={18} aria-hidden /> {t("pages.data.add_dataset")}
            </button>
          </>
        }
      />
      {error && <p className="form-error">{t(error)}</p>}
      {notice && <p className="form-ok">{notice}</p>}
      <IntakePanel
        intake={intake}
        items={items ?? []}
        onScan={() => void api.scanNow().then(() => window.setTimeout(loadIntake, 1500))}
        onRetry={(p) => void api.retryPackage(p.path).then(loadIntake).catch((err) => setError(errorKey(err)))}
      />
      {items && items.length === 0 && (
        <div className="panel">
          <EmptyState icon={Layers} accent="var(--c-data)" title={t("pages.data.empty_title")} body={t("data.empty_body")} />
        </div>
      )}
      {items && items.length > 0 && (
        <>
          <div className="toolbar">
            <label className="search-box">
              <Search size={16} aria-hidden />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("data.search_placeholder")} aria-label={t("data.search_placeholder")} />
            </label>
            <select className="select-box" value={category} onChange={(e) => setCategory(e.target.value)} aria-label={t("data.category_filter")}>
              <option value="">{t("data.all_categories")}</option>
              {categories(items).filter(Boolean).map((c) => <option key={c} value={c}>{f.category(c)}</option>)}
            </select>
            <span className="cell-muted">{t("data.result_count", { shown: filtered.length, total: items.length })}</span>
          </div>
          <div className="catalog-split">
            <div className="panel">
              <h2 className="panel-title">{t("data.catalog_title")}</h2>
              {filtered.length === 0 ? (
                <p className="muted small-gap">{t("data.no_match")}</p>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t("data.col.dataset")}</th>
                      <th>{t("data.col.version")}</th>
                      <th>{t("data.col.quality")}</th>
                      <th className="num">{t("data.col.features")}</th>
                      <th>{t("data.col.access")}</th>
                      <th>{t("data.col.registered")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((i) => (
                      <tr key={i.id} className={selected?.id === i.id ? "selected" : ""} onClick={() => setSelectedId(i.id)} tabIndex={0}
                        onKeyDown={(e) => e.key === "Enter" && setSelectedId(i.id)}>
                        <td>
                          <div className="cell-strong">{f.title(i)}</div>
                          <div className="cell-muted">{f.category(i.category)} · {i.package_id ?? f.unknown}</div>
                        </td>
                        <td className="mono">{i.version}</td>
                        <td><QualityPill status={i.quality_status} /></td>
                        <td className="num">{f.number(i.feature_count)}</td>
                        <td><AccessBadge access={i.access_class} /></td>
                        <td className="small-cell">{f.date(i.registered_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            {selected && <DetailPanel item={selected} onVerified={() => { loadCatalog(); setNotice(t("data.detail.verified")); }} />}
          </div>
        </>
      )}
      {dialog && (
        <PackageDialog
          mode={dialog}
          target={dialog === "version" ? selected : null}
          onClose={() => setDialog(null)}
          onSubmitted={() => { setDialog(null); setNotice(t("data.add.accepted")); window.setTimeout(loadIntake, 1000); }}
        />
      )}
    </RequireProject>
  );
}
