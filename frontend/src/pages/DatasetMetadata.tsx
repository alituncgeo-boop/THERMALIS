import { AlertTriangle, ArrowLeft, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, errorKey, type DatasetMetadata, type SemanticField, type SemanticVerification } from "../api";
import { PageHeader } from "../components/Layout";
import { RequireProject } from "../components/projects";
import { AccessBadge, QualityPill, useDataFormat } from "./DataCatalog";

type Row = Record<string, unknown>;

function Section({ title, children, collapsible = false, open = true }: { title: string; children: ReactNode; collapsible?: boolean; open?: boolean }) {
  if (collapsible) {
    return (
      <details className="panel meta-section" open={open}>
        <summary className="panel-title">{title}</summary>
        {children}
      </details>
    );
  }
  return (
    <section className="panel meta-section">
      <h2 className="panel-title">{title}</h2>
      {children}
    </section>
  );
}

function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([label, value]) => (
        <div className="kv-row" key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Table({ columns, rows, render }: { columns: { key: string; label: string; num?: boolean; mono?: boolean }[]; rows: Row[]; render?: (key: string, row: Row) => ReactNode }) {
  const f = useDataFormat();
  if (!rows.length) return <p className="muted small-gap">{f.t("data.value.none")}</p>;
  return (
    <div className="table-scroll">
      <table className="data-table compact">
        <thead>
          <tr>{columns.map((c) => <th key={c.key} className={c.num ? "num" : ""}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c.key} className={`${c.num ? "num" : ""} ${c.mono ? "mono small-cell" : ""}`}>
                  {render ? render(c.key, row) ?? cell(row[c.key], f) : cell(row[c.key], f)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function cell(value: unknown, f: ReturnType<typeof useDataFormat>): ReactNode {
  if (value === null || value === undefined || value === "") return <span className="cell-muted">—</span>;
  if (typeof value === "number") return f.number(value, 6);
  if (typeof value === "boolean") return value ? "✓" : "✗";
  return String(value);
}

/* ---------- anlamsal veri sözlüğü ---------- */
function SemanticStatus({ status }: { status: string | null }) {
  const { t } = useDataFormat();
  if (!status) return <span className="cell-muted">—</span>;
  return <span className={`sem-badge sem-${status.toLowerCase()}`}>{t(`data.semantic.status.${status}`, { defaultValue: status })}</span>;
}

function VerifyDialog({ field, onClose, onSaved }: { field: SemanticField; onClose: () => void; onSaved: () => void }) {
  const { t } = useDataFormat();
  const [meaning, setMeaning] = useState(field.meaning ?? "");
  const [unit, setUnit] = useState(field.unit ?? "");
  const [description, setDescription] = useState(field.description ?? "");
  const [nullMeaning, setNullMeaning] = useState("");
  const [note, setNote] = useState("");
  const [by, setBy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body: SemanticVerification = { dataset_id: field.layer_dataset_id, version: field.version, field: field.field };
    if (meaning.trim()) body.meaning = meaning.trim();
    if (unit.trim() && unit.trim() !== field.unit) body.unit = unit.trim();
    if (description.trim()) body.description = description.trim();
    if (nullMeaning.trim()) body.null_meaning = nullMeaning.trim();
    if (note.trim()) body.note = note.trim();
    if (by.trim()) body.verified_by = by.trim();
    try {
      await api.verifySemantic(body);
      onSaved();
    } catch (err) {
      setError(errorKey(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="sem-dialog-title" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="dialog-header">
          <h2 id="sem-dialog-title">{t("data.semantic.verify_title", { field: field.field })}</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label={t("common.cancel")}><X size={16} /></button>
        </div>
        <p className="muted small-gap">{t("data.semantic.verify_help")}</p>
        {field.status === "INFERRED_UNVERIFIED" && <p className="notice-warn">{t("data.semantic.proposal_note", { origin: field.origin ?? "" })}</p>}
        <label className="field">{t("data.semantic.meaning")}<textarea rows={3} value={meaning} onChange={(e) => setMeaning(e.target.value)} /></label>
        <label className="field">{t("data.semantic.unit")}<input value={unit} onChange={(e) => setUnit(e.target.value)} /></label>
        <label className="field">{t("data.semantic.description")}<textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} /></label>
        <label className="field">{t("data.semantic.null_meaning")}<input value={nullMeaning} onChange={(e) => setNullMeaning(e.target.value)} /></label>
        <label className="field">{t("data.semantic.note")}<input value={note} onChange={(e) => setNote(e.target.value)} /></label>
        <label className="field">{t("data.semantic.verified_by")}<input value={by} onChange={(e) => setBy(e.target.value)} /></label>
        {error && <p className="form-error">{t(error)}</p>}
        <div className="dialog-actions">
          <button type="button" className="ghost-button" onClick={onClose}>{t("common.cancel")}</button>
          <button type="submit" className="primary-button" disabled={busy || !meaning.trim()}>{t("data.semantic.save")}</button>
        </div>
      </form>
    </div>
  );
}

function SemanticSection({ datasetId, version }: { datasetId: string; version: string }) {
  const f = useDataFormat();
  const { t } = f;
  const [fields, setFields] = useState<SemanticField[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<SemanticField | null>(null);
  const load = useCallback(() => {
    api.semantics(datasetId, version).then((r) => setFields(r.fields)).catch((err) => setError(errorKey(err)));
  }, [datasetId, version]);
  useEffect(load, [load]);
  const counts = (fields ?? []).reduce<Record<string, number>>((acc, x) => ({ ...acc, [x.status]: (acc[x.status] ?? 0) + 1 }), {});

  return (
    <Section title={t("data.semantic.title", { count: fields?.length ?? 0 })}>
      <p className="muted small-gap">{t("data.semantic.note_status")}</p>
      {error && <p className="form-error">{t(error)}</p>}
      {fields && (
        <>
          <div className="chip-row">
            {(["SOURCE_DECLARED", "USER_VERIFIED", "INFERRED_UNVERIFIED", "UNKNOWN"] as const).map((s) => (
              <span key={s} className="chip">{t(`data.semantic.status.${s}`)}: {f.number(counts[s] ?? 0)}</span>
            ))}
          </div>
          <div className="table-scroll">
            <table className="data-table compact semantic-table">
              <thead>
                <tr>
                  <th>{t("data.meta.col.field")}</th>
                  <th>{t("data.meta.col.type")}</th>
                  <th>{t("data.semantic.meaning")}</th>
                  <th>{t("data.semantic.status_col")}</th>
                  <th>{t("data.semantic.unit")}</th>
                  <th>{t("data.meta.col.domain")}</th>
                  <th>{t("data.semantic.null_meaning")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {fields.map((x) => (
                  <tr key={x.field}>
                    <td className="mono small-cell">{x.field}{x.alias && x.alias !== x.field && <div className="cell-muted">{x.alias}</div>}</td>
                    <td className="small-cell">{x.data_type ?? "—"}</td>
                    <td className="small-cell" title={x.source_ref ?? undefined}>
                      {x.meaning ?? <span className="cell-muted">{t("data.value.unknown")}</span>}
                      {x.proposal_compatibility && <div className="cell-muted">{t(`data.semantic.compat.${x.proposal_compatibility}`, { defaultValue: x.proposal_compatibility })}</div>}
                      {x.verified_at && <div className="cell-muted">{t("data.semantic.verified_line", { by: x.verified_by ?? "—", at: f.date(x.verified_at) })}</div>}
                    </td>
                    <td><SemanticStatus status={x.status} /></td>
                    <td className="small-cell">{x.unit ? <>{x.unit} <SemanticStatus status={x.unit_status} /></> : <span className="cell-muted">—</span>}</td>
                    <td className="mono small-cell">{x.domain_name ?? "—"}</td>
                    <td className="small-cell">{x.null_meaning ? (x.null_meaning.startsWith("DOCUMENTED_SPECIAL_RECORDS:")
                      ? t("data.semantic.special_records", { count: Number(x.null_meaning.split(":")[1]) }) : x.null_meaning) : <span className="cell-muted">—</span>}</td>
                    <td>{x.data_type !== "OID" && x.data_type !== "Geometry" && (
                      <button type="button" className="ghost-button small-button" onClick={() => setEditing(x)}>{t("data.semantic.verify")}</button>
                    )}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {editing && <VerifyDialog field={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </Section>
  );
}

export function DatasetMetadataPage() {
  const f = useDataFormat();
  const { t } = f;
  const [params] = useSearchParams();
  const id = params.get("id") ?? "";
  const version = params.get("version") ?? undefined;
  const [meta, setMeta] = useState<DatasetMetadata | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [findingCategory, setFindingCategory] = useState("");

  useEffect(() => {
    setMeta(null);
    setError(null);
    api.dataset(id, version).then(setMeta).catch((err) => setError(errorKey(err)));
  }, [id, version]);

  const findings = useMemo(
    () => (meta?.quality.findings ?? []).filter((x) => !findingCategory || x.category === findingCategory),
    [meta, findingCategory],
  );

  const back = (
    <Link to="/data" className="back-link"><ArrowLeft size={16} aria-hidden /> {t("data.meta.back")}</Link>
  );
  if (error) return <RequireProject>{back}<p className="form-error">{t(error)}</p></RequireProject>;
  if (!meta) return <RequireProject>{back}<p className="muted">{t("data.meta.loading")}</p></RequireProject>;

  const { item, identity, source, crs, geometry, quality, access, integrity } = meta;
  const profile = source.profile ?? {};
  const lm = source.layer_metadata ?? {};
  const v = (value: unknown, fallback?: string) => f.text(value, fallback);
  const categoriesInFindings = [...new Set(meta.quality.findings.map((x) => x.category))].sort();

  return (
    <RequireProject>
      {back}
      <PageHeader lead={f.title(item)} accent={item.version} subtitle={item.id} />

      {meta.record_issues.length > 0 && (
        <Section title={t("data.meta.record_issues")}>
          <p className="notice-warn"><AlertTriangle size={14} aria-hidden /> {t("data.meta.record_issues_note")}</p>
          <Table
            columns={[{ key: "code", label: t("data.meta.col.code"), mono: true }, { key: "detail", label: t("data.meta.col.detail") }, { key: "evidence", label: t("data.meta.col.evidence"), mono: true }]}
            rows={meta.record_issues}
            render={(key, row) => {
              if (key === "code") return <span className="nowrap">{t(`data.meta.issue.${row.code}`, { defaultValue: String(row.code) })}</span>;
              if (key === "detail") return t(`data.meta.issue_detail.${row.code}`, { detail: String(row.detail ?? ""), defaultValue: String(row.detail ?? "") });
              return undefined;
            }}
          />
        </Section>
      )}

      <div className="meta-grid">
        <Section title={t("data.meta.identity")}>
          <KV rows={[
            [t("data.detail.dataset_id"), <span className="mono">{v(identity.id)}</span>],
            [t("data.meta.parent_dataset"), <span className="mono">{v(identity.dataset_id)}</span>],
            [t("data.detail.dataset_version"), <span className="mono">{v(identity.version)}</span>],
            [t("data.detail.package_version"), <span className="mono">{v(identity.package_version, f.unknown)}</span>],
            [t("data.meta.package_id"), <span className="mono">{v(identity.package_id)}</span>],
            [t("data.meta.fingerprint"), <span className="mono break">{v(identity.package_fingerprint, f.unknown)}</span>],
            [t("data.meta.ingest_id"), <span className="mono">{v(identity.ingest_id)}</span>],
            [t("data.detail.registered"), f.date(identity.registered_at as string)],
            [t("data.meta.tool"), `${v(identity.tool)} · GDAL ${v(identity.gdal, f.unknown)}`],
          ]} />
          <h3 className="sub-title">{t("data.meta.version_history")}</h3>
          <Table columns={[{ key: "version", label: t("data.col.version"), mono: true }, { key: "quality_status", label: t("data.col.quality") },
            { key: "package_id", label: t("data.meta.package_id"), mono: true }, { key: "registered_at", label: t("data.col.registered") }]}
            rows={meta.version_history}
            render={(key, row) => (key === "quality_status" ? <QualityPill status={String(row.quality_status)} /> : key === "registered_at" ? f.date(row.registered_at as string) : undefined)} />
        </Section>

        <Section title={t("data.meta.source")}>
          <KV rows={[
            [t("data.detail.source_institution"), v(item.source_institution)],
            [t("data.meta.owner"), v(source.owner)],
            [t("data.meta.origin_system"), v(profile.origin_system)],
            [t("data.detail.category"), f.category(item.category)],
            [t("data.meta.dataset_title"), v(profile.title)],
            [t("data.meta.summary"), v(lm.summary)],
            [t("data.detail.description"), v(lm.description)],
            [t("data.meta.tags"), v(lm.tags)],
            [t("data.detail.temporal"), f.temporal(item.temporal)],
            [t("data.meta.source_update_year"), v(source.source_update_year)],
            [t("data.meta.population_scope"), v(source.population_scope)],
            [t("data.meta.origin_path"), <span className="mono break">{v(source.origin_path)}</span>],
            [t("data.meta.ingested_from"), <span className="mono break">{v(source.ingested_from)}</span>],
            [t("data.meta.metadata_source"), <span className="mono break">{[lm.source_file, profile.source_file].filter(Boolean).join(" · ") || f.notProvided}</span>],
          ]} />
        </Section>

        <Section title={t("data.meta.access")}>
          <KV rows={[
            [t("data.detail.access"), <AccessBadge access={String(access.classification ?? "UNKNOWN")} />],
            [t("data.meta.public_release"), access.public_release === false ? t("data.meta.no") : access.public_release === true ? t("data.meta.yes") : f.unknown],
            [t("data.meta.authorization"), v(access.authorization)],
            [t("data.meta.policy"), v(access.policy)],
            [t("data.meta.constraints"), v(access.constraints_text)],
          ]} />
        </Section>

        <Section title={t("data.meta.crs")}>
          <KV rows={[
            [t("data.detail.crs"), <span className="mono">{v(crs?.native_crs_name, f.unknown)}</span>],
            [t("data.meta.epsg"), crs?.factory_code === 0 || crs?.factory_code === "0" ? t("data.meta.epsg_none") : v(crs?.factory_code, f.unknown)],
            [t("data.meta.unit"), v(crs?.unit, f.unknown)],
            [t("data.meta.transformed"), crs?.transformed ? t("data.meta.yes") : t("data.meta.no")],
            [t("data.meta.crs_policy"), v(crs?.policy)],
          ]} />
          <pre className="wkt">{v(crs?.native_wkt, f.unknown)}</pre>
        </Section>

        <Section title={t("data.meta.geometry")}>
          <KV rows={[
            [t("data.col.geometry"), v(geometry.type, f.unknown)],
            [t("data.col.features"), `${f.number(geometry.feature_count as number)} / ${t("data.meta.expected")} ${f.number(geometry.expected_feature_count as number)}`],
            [t("data.meta.null_geometry"), f.number(geometry.null_geometry as number)],
            [t("data.meta.empty_geometry"), f.number(geometry.empty_geometry as number)],
            [t("data.detail.spatial"), f.extent(geometry.extent)],
            [t("data.meta.crs_matches"), geometry.crs_matches_package === 1 ? t("data.meta.yes") : geometry.crs_matches_package === 0 ? t("data.meta.no") : f.unknown],
          ]} />
        </Section>
      </div>

      <Section title={t("data.meta.fields", { count: meta.fields.length })}>
        <Table columns={[{ key: "name", label: t("data.meta.col.field"), mono: true }, { key: "dictionary_alias", label: t("data.meta.col.alias") },
          { key: "type", label: t("data.meta.col.type") }, { key: "dictionary_type", label: t("data.meta.col.dictionary_type") },
          { key: "domain", label: t("data.meta.col.domain"), mono: true }, { key: "nullable", label: t("data.meta.col.nullable") },
          { key: "null_count", label: t("data.meta.col.null_count"), num: true }]}
          rows={meta.fields}
          render={(key, row) => (key === "nullable" ? (row.nullable === null ? "—" : row.nullable ? t("data.meta.yes") : t("data.meta.no")) : undefined)} />
        <p className="muted small">{t("data.meta.null_note")}</p>
      </Section>

      <SemanticSection datasetId={item.id} version={item.version} />

      <Section title={t("data.meta.dictionary", { count: meta.dictionary.length })} collapsible open={false}>
        <Table columns={[{ key: "field", label: t("data.meta.col.field"), mono: true }, { key: "alias", label: t("data.meta.col.alias") },
          { key: "type", label: t("data.meta.col.type") }, { key: "length", label: t("data.meta.col.length"), num: true },
          { key: "nullable", label: t("data.meta.col.nullable") }, { key: "required", label: t("data.meta.col.required") },
          { key: "coded_domain", label: t("data.meta.col.domain"), mono: true }, { key: "description", label: t("data.detail.description") }]}
          rows={meta.dictionary} />
        <p className="muted small">{t("data.meta.semantic_note")}</p>
      </Section>

      <Section title={t("data.meta.domains", { count: meta.domains.length })} collapsible open={false}>
        {meta.domains.map((d) => (
          <details key={d.name} className="domain-block">
            <summary><span className="mono">{d.name}</span> · {v(d.description)} · {t("data.meta.codes", { count: d.values.length })}</summary>
            <Table columns={[{ key: "code", label: t("data.meta.col.code"), mono: true }, { key: "label", label: t("data.meta.col.label") }]} rows={d.values} />
          </details>
        ))}
        {!meta.domains.length && <p className="muted small-gap">{t("data.value.none")}</p>}
      </Section>

      <Section title={t("data.meta.quality")}>
        <div className="quality-head">
          <QualityPill status={quality.status} />
          <span className="muted small-cell">{v(quality.upgrade_policy)}</span>
        </div>
        <KV rows={[
          [t("data.meta.status_source"), <span className="mono">{v(quality.status_source)}</span>],
          [t("data.meta.steward"), v(quality.steward_disposition)],
          [t("data.meta.fitness"), v(quality.fitness_for_use)],
          [t("data.meta.finding_totals"), t("data.meta.finding_totals_value", {
            records: quality.version_summary.records, unique: quality.version_summary.unique_exception_ids,
            register: quality.version_summary.by_origin.package_exception_register ?? 0, derived: quality.version_summary.by_origin.ingest_derived ?? 0 })],
          [t("data.meta.duplicates"), quality.version_summary.duplicated_exception_ids.length ? <span className="form-error mono">{quality.version_summary.duplicated_exception_ids.join(", ")}</span> : t("data.value.none")],
        ]} />
        {quality.gates.length > 0 && (
          <div className="gates">
            {quality.gates.map((g) => <span key={String(g.gate_id)} className={`gate gate-${String(g.status).toLowerCase()}`} title={String(g.detail ?? "")}>{String(g.gate_id)} · {String(g.status)}</span>)}
          </div>
        )}
        <div className="toolbar">
          <h3 className="sub-title">{t("data.meta.findings_for_layer", { count: findings.length })}</h3>
          <select className="select-box" value={findingCategory} onChange={(e) => setFindingCategory(e.target.value)} aria-label={t("data.meta.finding_filter")}>
            <option value="">{t("data.meta.all_findings")}</option>
            {categoriesInFindings.map((c) => <option key={c} value={c}>{t(`data.category.${c}`, { defaultValue: c })}</option>)}
          </select>
        </div>
        <Table
          columns={[{ key: "exception_id", label: t("data.meta.col.id"), mono: true }, { key: "category", label: t("data.meta.col.category") },
            { key: "feature_a", label: "A" }, { key: "oid_a", label: "OID A", num: true }, { key: "feature_b", label: "B" }, { key: "oid_b", label: "OID B", num: true },
            { key: "area_m2", label: t("data.meta.col.area"), num: true }, { key: "disposition", label: t("data.meta.col.disposition"), mono: true },
            { key: "origin", label: t("data.meta.col.origin") }]}
          rows={findings as unknown as Row[]}
          render={(key, row) => {
            if (key === "exception_id") return <span className={row.id_unique ? "" : "form-error"} title={row.id_unique ? undefined : t("data.meta.duplicate_id")}>{String(row.exception_id)}{row.id_unique ? "" : " ⚠"}</span>;
            if (key === "category") return t(`data.category.${row.category}`, { defaultValue: String(row.category) });
            if (key === "origin") return t(`data.meta.origin.${row.origin}`, { defaultValue: String(row.origin) });
            return undefined;
          }}
        />
        {quality.layer_status.length > 0 && (
          <>
            <h3 className="sub-title">{t("data.meta.layer_status")}</h3>
            <Table columns={[{ key: "layer_name", label: t("data.col.layer"), mono: true }, { key: "geometry_state", label: t("data.meta.col.geometry_state"), mono: true },
              { key: "record_location", label: t("data.meta.col.evidence"), mono: true }]} rows={quality.layer_status} />
          </>
        )}
      </Section>

      <div className="meta-grid">
        <Section title={t("data.meta.related")}>
          <Table columns={[{ key: "layer_name", label: t("data.col.layer"), mono: true }, { key: "role", label: t("data.col.role") },
            { key: "feature_count", label: t("data.col.features"), num: true }, { key: "shares_asset", label: t("data.meta.col.shares_asset") }]}
            rows={meta.related_datasets as unknown as Row[]}
            render={(key, row) => {
              if (key === "role") return t(`data.role.${row.role}`, { defaultValue: String(row.role) });
              if (key === "layer_name" && row.published) return <Link to={`/data/dataset?${new URLSearchParams({ id: String(row.id), version: item.version })}`}>{String(row.layer_name)}</Link>;
              return undefined;
            }} />
        </Section>

        <Section title={t("data.meta.integrity")}>
          <KV rows={[
            [t("data.detail.integrity"), v(integrity.status, f.unknown)],
            [t("data.detail.last_success_label"), f.date(integrity.last_successful_at as string)],
            [t("data.meta.files"), `${f.number(integrity.file_count as number)} · ${f.number(integrity.total_bytes as number)} B · ${v(integrity.algorithm)}`],
            [t("data.meta.chain"), Object.entries(integrity.verifications).map(([k, s]) => `${t(`data.meta.verification.${k}`, { defaultValue: k })}: ${s}`).join(" · ")],
          ]} />
          <Table columns={[{ key: "at", label: t("data.meta.col.at") }, { key: "kind", label: t("data.meta.col.kind") }, { key: "status", label: t("data.intake.col.state") }, { key: "detail", label: t("data.meta.col.detail") }]}
            rows={integrity.history}
            render={(key, row) => (key === "at" ? f.date(row.at as string) : key === "kind" ? t(`data.meta.verification_kind.${row.kind}`, { defaultValue: String(row.kind) }) : undefined)} />
        </Section>
      </div>

      <Section title={t("data.meta.completeness")} collapsible open={false}>
        <Table columns={[{ key: "item", label: t("data.meta.col.item") }, { key: "state", label: t("data.intake.col.state") }, { key: "detail", label: t("data.meta.col.detail") }]}
          rows={meta.metadata_completeness as unknown as Row[]}
          render={(key, row) => (key === "item" ? t(`data.meta.completeness_item.${row.item}`, { defaultValue: String(row.item) }) : key === "state" ? t(`data.meta.state.${row.state}`, { defaultValue: String(row.state) }) : undefined)} />
      </Section>

      <Section title={t("data.meta.assets", { count: meta.assets.length })} collapsible open={false}>
        <Table columns={[{ key: "path", label: t("data.meta.col.path"), mono: true }, { key: "kind", label: t("data.meta.col.kind") },
          { key: "bytes", label: t("data.meta.col.bytes"), num: true }, { key: "sha256", label: "SHA-256", mono: true }]}
          rows={meta.assets}
          render={(key, row) => (key === "kind" ? t(`data.meta.asset_kind.${row.kind}`, { defaultValue: String(row.kind) }) : undefined)} />
      </Section>

      <Section title={t("data.meta.checks", { count: meta.registration_checks.length })} collapsible open={false}>
        <Table columns={[{ key: "id", label: t("data.meta.col.code"), mono: true }, { key: "title", label: t("data.meta.col.check") },
          { key: "status", label: t("data.intake.col.state") }, { key: "detail", label: t("data.meta.col.detail") }]} rows={meta.registration_checks} />
      </Section>
    </RequireProject>
  );
}
