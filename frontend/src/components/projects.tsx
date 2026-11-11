import { ChevronDown, FolderOpen, MapPin, Plus, X } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { errorKey } from "../api";
import { useApp } from "../state";
import { EmptyState } from "./common";

export function ProjectSelect({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  const { projects, activeProject, selectProject } = useApp();
  return (
    <label className={`project-select${compact ? " compact" : ""}`}>
      <MapPin size={18} aria-hidden />
      <select
        aria-label={t("common.active_project")}
        value={activeProject?.id ?? ""}
        onChange={(e) => e.target.value && void selectProject(e.target.value)}
      >
        <option value="" disabled>
          {projects.length ? t("home.select_city") : t("home.no_projects")}
        </option>
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      <ChevronDown size={16} aria-hidden className="chevron" />
    </label>
  );
}

export function CreateProjectButton() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="ghost-button" onClick={() => setOpen(true)}>
        <Plus size={18} aria-hidden />
        {t("home.create_city")}
      </button>
      {open && <CreateProjectDialog onClose={() => setOpen(false)} />}
    </>
  );
}

function CreateProjectDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const { createProject, settings } = useApp();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createProject(name, description);
      onClose();
    } catch (err) {
      setError(errorKey(err));
    } finally {
      setBusy(false);
    }
  }

  // Portal: üst öğelerdeki efektler (backdrop-filter) pencerenin konumunu bozmasın.
  return createPortal(
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="new-project-title" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="dialog-header">
          <h2 id="new-project-title">{t("project.dialog_title")}</h2>
          <button type="button" className="icon-button" aria-label={t("common.cancel")} onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        <label className="field">
          <span>{t("project.name_label")}</span>
          <input autoFocus required maxLength={80} value={name} placeholder={t("project.name_placeholder")} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="field">
          <span>{t("project.description_label")}</span>
          <textarea maxLength={500} rows={3} value={description} placeholder={t("project.description_placeholder")} onChange={(e) => setDescription(e.target.value)} />
        </label>
        {settings && <p className="muted small">{t("project.data_root_note", { path: settings.data_root })}</p>}
        {error && <p className="form-error">{t(error)}</p>}
        <div className="dialog-actions">
          <button type="button" className="ghost-button" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="primary-button" disabled={busy || !name.trim()}>
            {t("common.create")}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}

/** Aktif proje yoksa sayfa yerine dürüst bir yönlendirme gösterir. */
export function RequireProject({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { activeProject } = useApp();
  if (!activeProject) {
    return (
      <EmptyState icon={FolderOpen} title={t("project.none_title")} body={t("project.none_body")}>
        <Link to="/" className="primary-button">
          {t("project.go_home")}
        </Link>
      </EmptyState>
    );
  }
  return <>{children}</>;
}
