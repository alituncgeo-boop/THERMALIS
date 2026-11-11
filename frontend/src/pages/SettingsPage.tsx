import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { errorKey } from "../api";
import { LanguageToggle } from "../components/common";
import { PageHeader } from "../components/Layout";
import { useApp } from "../state";

export function SettingsPage() {
  const { t } = useTranslation();
  const { settings, setDataRoot, version } = useApp();
  const [path, setPath] = useState(settings?.data_root ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => setPath(settings?.data_root ?? ""), [settings?.data_root]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaved(false);
    try {
      await setDataRoot(path);
      setSaved(true);
    } catch (err) {
      setError(errorKey(err));
    }
  }

  return (
    <>
      <PageHeader accent={t("common.settings")} />
      <div className="panel settings-panel">
        <h2 className="panel-title">{t("common.language")}</h2>
        <p className="muted">{t("pages.settings.language_help")}</p>
        <LanguageToggle />
      </div>
      <form className="panel settings-panel" onSubmit={submit}>
        <h2 className="panel-title">
          {t("pages.settings.data_root_label")}
          {settings?.data_root_is_default && <span className="badge">{t("pages.settings.default_badge")}</span>}
        </h2>
        <p className="muted">{t("pages.settings.data_root_help")}</p>
        <div className="inline-form">
          <input className="path-input" value={path} onChange={(e) => setPath(e.target.value)} spellCheck={false} aria-label={t("pages.settings.data_root_label")} />
          <button type="submit" className="primary-button" disabled={!path.trim() || path === settings?.data_root}>
            {t("common.save")}
          </button>
        </div>
        <p className="muted small">{t("pages.settings.data_root_warning")}</p>
        {error && <p className="form-error">{t(error)}</p>}
        {saved && <p className="form-ok">{t("common.saved")}</p>}
      </form>
      {version && (
        <p className="muted small version-line">
          THERMALIS · {t("pages.settings.version")} {version}
        </p>
      )}
    </>
  );
}
