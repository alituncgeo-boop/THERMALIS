import {
  AlertTriangle, ArrowDown, ArrowUp, Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Copy, Download,
  FileSearch, Layers, Lock, Plus, RefreshCw, Search, ShieldCheck, Upload, X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { api, errorKey, type CatalogItem, type IntakePackage, type IntakeStatus } from "../api";
import { EmptyState } from "../components/common";
import { PageHeader } from "../components/Layout";
import { RequireProject } from "../components/projects";
import { useApp } from "../state";
import { categories, filterCatalog, intakeBuckets, isMissing, latestRegistered, pageOf, sortCatalog, type SortKey } from "./dataModel";

const POLL_MS = 5000;
const PAGE_SIZE = 25;
const RETRYABLE = ["BLOCKED", "ADAPTER_REQUIRED", "FAILED"];
// Çevirisi tam açıklama olan kodlar: ham ayrıntı yalnız ipucunda (kanıt taşıyan kodlarda ikinci satırda kalır).
const SELF_EXPLANATORY = ["ARCHIVE", "ALREADY_REGISTERED", "STABILITY_CHECK", "VALIDATING", "INGESTING", "RETRY_REQUESTED", "INTERRUPTED"];

/* ---------- ortak biçimlendirme ---------- */
export function useDataFormat() {
  const { t, i18n } = useTranslation();
  const locale = i18n.language === "tr" ? "tr-TR" : "en-GB";
  return useMemo(() => {
    const unknown = t("data.value.unknown");
    const notProvided = t("data.value.not_provided");
    const number = (n: number | null | undefined, digits = 0) =>
      isMissing(n) ? unknown : (n as number).toLocaleString(locale, { maximumFractionDigits: digits });
    const date = (iso: string | null | undefined) =>
      isMissing(iso) ? unknown : new Date(iso as string).toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
    const shortDate = (iso: string | null | undefined) =>
      isMissing(iso) ? unknown : new Date(iso as string).toLocaleDateString(locale, { dateStyle: "medium" });
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
    return { t, locale, unknown, notProvided, number, date, shortDate, text, title, category, temporal, extent };
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

/** Uzun kimlik: tek satır, gerekirse kısaltılır; tam değer ipucunda ve kopyalanabilir. */
function IdLine({ value }: { value: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* pano erişimi yoksa sessizce geç: değer zaten seçilebilir */
    }
  }
  return (
    <div className="id-line">
      <span className="mono ellipsis" title={value}>{value}</span>
      <button type="button" className="icon-button tiny" onClick={() => void copy()} title={t("data.detail.copy_id")} aria-label={t("data.detail.copy_id")}>
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
}

/* ---------- veri kabul şeridi (kompakt, genişletilebilir) ---------- */
function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: "warn" | "bad" | "muted" }) {
  return (
    <div className={`metric ${tone ?? ""}`}>
      <span className="metric-value">{value}</span>
      <span className="metric-label">{label}</span>
    </div>
  );
}

function IntakeStrip({ intake, items, onScan, onRetry }: { intake: IntakeStatus | null; items: CatalogItem[]; onScan: () => void; onRetry: (p: IntakePackage) => void }) {
  const f = useDataFormat();
  const { t } = f;
  const [open, setOpen] = useState(false);
  const counts = intake?.counts ?? {};
  const b = intakeBuckets(counts);
  const latest = latestRegistered(items);
  const w = intake?.watcher;
  const visible = (intake?.packages ?? []).filter((p) => p.present !== false);
  const folder = w?.incoming ?? "";
  const watchState = !w ? "unknown" : !w.enabled ? "off" : w.last_result && w.last_result !== "ok" ? "warn" : "on";

  return (
    <section className="panel intake-strip" aria-label={t("data.intake.title")}>
      <div className="intake-row">
        <div className="intake-source">
          <span className={`watch-dot w-${watchState}`} aria-hidden />
          <div className="intake-source-text">
            <span className="intake-title">{t("data.intake.title")}</span>
            <span className="cell-muted ellipsis" title={folder}>
              {!w ? f.unknown : !w.enabled ? t("data.intake.watch_off") : t("data.intake.watching", { folder: folder.split(/[\\/]/).filter(Boolean).pop() ?? folder, seconds: w.interval_seconds })}
              {w?.last_scan_finished && ` · ${t("data.intake.last_scan", { at: f.date(w.last_scan_finished) })}`}
            </span>
            {w?.last_result && w.last_result !== "ok" && (
              <span className="form-error small-text">{t(`data.intake.result.${w.last_result}`, { defaultValue: w.last_result })}</span>
            )}
          </div>
        </div>
        <div className="metrics">
          <Metric label={t("data.intake.stat.registered")} value={f.number(items.length)} />
          <Metric label={t("data.intake.stat.in_progress")} value={f.number(b.inProgress)} tone={b.inProgress ? undefined : "muted"} />
          <Metric label={t("data.intake.stat.waiting")} value={f.number(b.waiting)} tone={b.waiting ? undefined : "muted"} />
          <Metric label={t("data.intake.stat.adapter_required")} value={f.number(b.adapterRequired)} tone={b.adapterRequired ? "warn" : "muted"} />
          <Metric label={t("data.intake.stat.failed")} value={f.number(b.failed)} tone={b.failed ? "bad" : "muted"} />
        </div>
        <div className="intake-actions">
          <button type="button" className="ghost-button small-button" onClick={onScan} disabled={!w?.enabled} title={t("data.intake.scan_now")}>
            <RefreshCw size={14} aria-hidden /> {t("data.intake.scan_now")}
          </button>
          <button type="button" className="ghost-button small-button" onClick={() => setOpen(!open)} aria-expanded={open} disabled={!visible.length}>
            {open ? <ChevronUp size={14} aria-hidden /> : <ChevronDown size={14} aria-hidden />} {t("data.intake.details", { count: visible.length })}
          </button>
        </div>
      </div>
      <div className="intake-footnote cell-muted">
        {latest ? t("data.intake.latest_line", { title: f.title(latest), at: f.date(latest.registered_at) }) : t("data.intake.no_integration")}
        {b.ignored > 0 && <> · {t("data.intake.ignored_line", { count: b.ignored })}</>}
      </div>
      {open && (
        <div className="table-scroll intake-details">
          <table className="data-table compact intake-table">
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
                  <td className="mono small-cell" title={p.path}>{p.name}{p.source === "manual" && <span className="cell-muted"> · {t("data.intake.manual")}</span>}</td>
                  <td><StateBadge state={p.state} /></td>
                  <td className="mono small-cell">{p.dataset_id ? `${p.dataset_id} ${p.version ?? ""}` : "—"}</td>
                  <td className="small-cell" title={p.reason || undefined}>
                    {p.reason_code ? t(`data.intake.reason.${p.reason_code}`, { defaultValue: p.reason_code }) : !p.reason && "—"}
                    {p.reason && !SELF_EXPLANATORY.includes(p.reason_code ?? "") && <div className="cell-muted">{p.reason}</div>}
                    {p.user_decision && <div className="cell-muted">{p.user_decision}</div>}
                    {p.support_gap_path && <div className="cell-muted mono">{p.support_gap_path}</div>}
                  </td>
                  <td className="small-cell">{f.date(p.updated_at)}</td>
                  <td>{RETRYABLE.includes(p.state) && p.item_id && <button type="button" className="ghost-button small-button" onClick={() => onRetry(p)}>{t("data.intake.retry")}</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/* ---------- seçili veri seti paneli ---------- */
function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="detail-group">
      <h3 className="group-title">{title}</h3>
      <dl className="kv">{children}</dl>
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="kv-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Clamp({ text }: { text: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const long = text.length > 220;
  return (
    <>
      <span className={!open && long ? "clamp-4" : ""}>{text}</span>
      {long && <button type="button" className="link-button" onClick={() => setOpen(!open)}>{open ? t("data.detail.show_less") : t("data.detail.show_more")}</button>}
    </>
  );
}

function TechnicalDetails({ item }: { item: CatalogItem }) {
  const { t } = useTranslation();
  const [wkt, setWkt] = useState<string | null | undefined>(undefined);
  const load = () => {
    if (wkt !== undefined) return;
    api.dataset(item.id, item.version).then((m) => setWkt((m.crs?.native_wkt as string | null) ?? null)).catch(() => setWkt(null));
  };
  useEffect(() => setWkt(undefined), [item.id, item.version]);
  return (
    <details className="detail-expand" onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && load()}>
      <summary>{t("data.detail.technical_details")}</summary>
      <dl className="kv">
        <Fact label={t("data.meta.fingerprint")}><span className="mono break">{item.package_fingerprint ?? t("data.value.unknown")}</span></Fact>
        <Fact label={t("data.detail.asset")}><span className="mono break">{item.asset_path ?? t("data.value.unknown")}</span></Fact>
      </dl>
      <pre className="wkt">{wkt === undefined ? t("data.meta.loading") : wkt ?? t("data.value.unknown")}</pre>
    </details>
  );
}

function DetailPanel({ item, onVerified }: { item: CatalogItem; onVerified: () => void }) {
  const f = useDataFormat();
  const { t } = f;
  const navigate = useNavigate();
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mc = item.metadata_completeness;
  const completeShare = mc.total ? Math.round((mc.present / mc.total) * 100) : 0;
  useEffect(() => setError(null), [item.id]);

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
    <aside className="panel detail-panel" aria-label={t("data.detail.panel_label")}>
      <header className="detail-head">
        <span className="eyebrow">{f.category(item.category)}</span>
        <h2 className="detail-title">{f.title(item)}</h2>
        <IdLine value={item.id} />
        <div className="chip-row">
          <span className="chip mono" title={t("data.detail.dataset_version")}>v{item.version}</span>
          <QualityPill status={item.quality_status} />
          <AccessBadge access={item.access_class} />
        </div>
        {item.record_issue_count > 0 && (
          <p className="notice-warn"><AlertTriangle size={14} aria-hidden /> {t("data.detail.record_issues", { count: item.record_issue_count })}</p>
        )}
      </header>

      <div className="detail-body">
        <Group title={t("data.detail.group.identity")}>
          <Fact label={t("data.detail.dataset_version")}><span className="mono">{item.version}</span>{item.version_count > 1 && <span className="cell-muted"> · {t("data.detail.versions", { count: item.version_count })}</span>}</Fact>
          <Fact label={t("data.detail.package_version")}>
            <span className="mono">{f.text(item.package_version, f.unknown)}</span>
            <div className="cell-muted mono ellipsis" title={item.package_id ?? ""}>{f.text(item.package_id, f.unknown)}</div>
          </Fact>
          <Fact label={t("data.detail.registered")}>{f.date(item.registered_at)}</Fact>
        </Group>

        <Group title={t("data.detail.group.source")}>
          <Fact label={t("data.detail.source_institution")}>{f.text(item.source_institution)}</Fact>
          <Fact label={t("data.detail.description")}>{isMissing(item.description ?? item.summary) ? f.notProvided : <Clamp text={(item.description ?? item.summary) as string} />}</Fact>
        </Group>

        <Group title={t("data.detail.group.technical")}>
          <Fact label={t("data.detail.format")}>{item.format_key ? t(`data.format.${item.format_key}`, { defaultValue: item.format ?? item.format_key }) : f.unknown}</Fact>
          <Fact label={t("data.detail.geometry")}>{t("data.detail.geometry_value", { geometry: f.text(item.geometry_type, f.unknown), n: f.number(item.feature_count) })}</Fact>
          <Fact label={t("data.detail.crs")}><span className="mono">{f.text(item.crs_name, f.unknown)}</span></Fact>
          <Fact label={t("data.detail.spatial")}>{f.extent(item.spatial_extent)}</Fact>
          <Fact label={t("data.detail.temporal")}>{f.temporal(item.temporal)}</Fact>
        </Group>

        <Group title={t("data.detail.group.status")}>
          <Fact label={t("data.detail.integrity")}>
            {item.integrity.status ? <span className={`gate gate-${item.integrity.status === "PASS" ? "pass" : "hold"}`}>{item.integrity.status}</span> : f.unknown}
            <div className="cell-muted">{t("data.detail.last_success", { at: f.date(item.integrity.last_successful_at) })}</div>
          </Fact>
          <Fact label={t("data.detail.completeness")}>
            <div className="meter" role="img" aria-label={`${completeShare}%`}><span style={{ width: `${completeShare}%` }} /></div>
            <div className="cell-muted">{t("data.detail.completeness_value", { present: mc.present, total: mc.total, partial: mc.partial, missing: mc.missing + mc.unknown })}</div>
          </Fact>
        </Group>

        <TechnicalDetails item={item} />
      </div>

      {error && <p className="form-error">{t(error)}</p>}
      <footer className="detail-actions">
        <button type="button" className="primary-button" onClick={() => navigate(`/data/dataset?${new URLSearchParams({ id: item.id, version: item.version })}`)}>
          <FileSearch size={17} aria-hidden /> {t("data.detail.view_metadata")}
        </button>
        <button type="button" className="ghost-button outlined" onClick={() => void verify()} disabled={verifying}>
          <ShieldCheck size={17} aria-hidden /> {verifying ? t("data.detail.verifying") : t("data.detail.verify")}
        </button>
      </footer>
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

/* ---------- katalog tablosu ---------- */
function SortHeader({ k, label, sort, onSort, className }: { k: SortKey; label: string; sort: { key: SortKey; dir: "asc" | "desc" }; onSort: (k: SortKey) => void; className?: string }) {
  const active = sort.key === k;
  return (
    <th className={className} aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" className={`sort-button ${active ? "active" : ""}`} onClick={() => onSort(k)}>
        {label}
        {active && (sort.dir === "asc" ? <ArrowUp size={12} aria-hidden /> : <ArrowDown size={12} aria-hidden />)}
      </button>
    </th>
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
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "title", dir: "asc" });
  const [page, setPage] = useState(0);
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

  const filtered = useMemo(() => {
    const matched = filterCatalog(items ?? [], query, category, (i) => [f.title(i), f.category(i.category)]);
    return sortCatalog(matched, sort.key, sort.dir, (i) => f.title(i), f.locale);
  }, [items, query, category, sort, f]);
  useEffect(() => setPage(0), [query, category, sort]);
  const { rows, from, to, pages } = pageOf(filtered, page, PAGE_SIZE);
  const selected = filtered.find((i) => i.id === selectedId) ?? rows[0] ?? null;

  function onSort(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "registered" || key === "features" ? "desc" : "asc" }));
  }

  function onRowKey(e: React.KeyboardEvent, index: number) {
    if (e.key === "Enter") setSelectedId(rows[index].id);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = rows[index + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) {
        setSelectedId(next.id);
        (e.currentTarget.parentElement?.children[index + (e.key === "ArrowDown" ? 1 : -1)] as HTMLElement | undefined)?.focus();
      }
    }
  }

  return (
    <RequireProject>
      <PageHeader
        lead="THERMALIS"
        accent="DATA"
        subtitle={t("pages.data.subtitle", { project })}
        actions={
          <>
            <button type="button" className="ghost-button outlined" disabled={!selected} onClick={() => setDialog("version")} title={selected ? undefined : t("data.add.select_first")}>
              <Upload size={17} aria-hidden /> {t("data.actions.upload_version")}
            </button>
            <div className="menu-anchor">
              <button type="button" className="ghost-button outlined" aria-expanded={exportOpen} aria-haspopup="menu" onClick={() => setExportOpen(!exportOpen)} disabled={!items?.length}>
                <Download size={17} aria-hidden /> {t("data.actions.export")}
              </button>
              {exportOpen && (
                <div className="menu" role="menu">
                  <a role="menuitem" href={api.exportUrl("json")} download onClick={() => setExportOpen(false)}>JSON</a>
                  <a role="menuitem" href={api.exportUrl("csv")} download onClick={() => setExportOpen(false)}>CSV</a>
                </div>
              )}
            </div>
            <button type="button" className="primary-button" onClick={() => setDialog("add")}>
              <Plus size={17} aria-hidden /> {t("pages.data.add_dataset")}
            </button>
          </>
        }
      />
      {error && <p className="form-error">{t(error)}</p>}
      {notice && <p className="form-ok">{notice}</p>}
      <IntakeStrip
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
        <div className="catalog-layout">
          <section className="panel catalog-panel" aria-label={t("data.catalog_title")}>
            <div className="catalog-head">
              <h2 className="panel-title">{t("data.catalog_title")} <span className="count-badge">{f.number(items.length)}</span></h2>
              <div className="toolbar">
                <label className="search-box">
                  <Search size={16} aria-hidden />
                  <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("data.search_short")} aria-label={t("data.search_placeholder")} title={t("data.search_placeholder")} />
                  {query && <button type="button" className="icon-button tiny" onClick={() => setQuery("")} aria-label={t("data.clear_search")}><X size={13} /></button>}
                </label>
                <select className="select-box" value={category} onChange={(e) => setCategory(e.target.value)} aria-label={t("data.category_filter")}>
                  <option value="">{t("data.all_categories")}</option>
                  {categories(items).filter(Boolean).map((c) => <option key={c} value={c}>{f.category(c)}</option>)}
                </select>
              </div>
            </div>
            {filtered.length === 0 ? (
              <p className="muted small-gap">{t("data.no_match")}</p>
            ) : (
              <div className="table-scroll catalog-scroll">
                <table className="data-table catalog-table">
                  <colgroup>
                    <col className="c-name" /><col className="c-version" /><col className="c-quality" /><col className="c-num" /><col className="c-access" /><col className="c-date" />
                  </colgroup>
                  <thead>
                    <tr>
                      <SortHeader k="title" label={t("data.col.dataset")} sort={sort} onSort={onSort} />
                      <SortHeader k="version" label={t("data.col.version")} sort={sort} onSort={onSort} />
                      <SortHeader k="quality" label={t("data.col.quality")} sort={sort} onSort={onSort} />
                      <SortHeader k="features" label={t("data.col.features")} sort={sort} onSort={onSort} className="num" />
                      <th>{t("data.col.access")}</th>
                      <SortHeader k="registered" label={t("data.col.registered")} sort={sort} onSort={onSort} />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((i, index) => (
                      <tr key={i.id} className={selected?.id === i.id ? "selected" : ""} aria-selected={selected?.id === i.id}
                        onClick={() => setSelectedId(i.id)} tabIndex={0} onKeyDown={(e) => onRowKey(e, index)}>
                        <td>
                          <div className="cell-strong clamp-2" title={f.title(i)}>{f.title(i)}</div>
                          <div className="cell-muted ellipsis" title={i.id}>{f.category(i.category)} · <span className="mono">{i.id.split("/").slice(1).join("/")}</span></div>
                        </td>
                        <td className="mono small-cell" title={i.version}>{i.version}</td>
                        <td><QualityPill status={i.quality_status} /></td>
                        <td className="num">{f.number(i.feature_count)}</td>
                        <td>{i.access_class === "RESTRICTED" ? <span className="access-icon" title={t("data.access.RESTRICTED")}><Lock size={14} aria-label={t("data.access.RESTRICTED")} /></span> : <span className="cell-muted small-cell">{t(`data.access.${i.access_class}`, { defaultValue: i.access_class })}</span>}</td>
                        <td className="small-cell" title={f.date(i.registered_at)}>{f.shortDate(i.registered_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="pager">
              <span className="cell-muted">{t("data.pager.range", { from: f.number(from), to: f.number(to), total: f.number(filtered.length), all: f.number(items.length) })}</span>
              {pages > 1 && (
                <div className="pager-buttons">
                  <button type="button" className="icon-button tiny" onClick={() => setPage(page - 1)} disabled={page === 0} aria-label={t("data.pager.prev")}><ChevronLeft size={14} /></button>
                  <span className="small-cell">{t("data.pager.page", { page: page + 1, pages })}</span>
                  <button type="button" className="icon-button tiny" onClick={() => setPage(page + 1)} disabled={page >= pages - 1} aria-label={t("data.pager.next")}><ChevronRight size={14} /></button>
                </div>
              )}
            </div>
          </section>
          {selected && <DetailPanel item={selected} onVerified={() => { loadCatalog(); setNotice(t("data.detail.verified")); }} />}
        </div>
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
