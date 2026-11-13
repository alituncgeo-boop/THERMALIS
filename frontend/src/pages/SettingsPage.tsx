import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { errorKey } from "../api";
import { LanguageToggle } from "../components/common";
import { PageHeader } from "../components/Layout";
import { useApp } from "../state";

function TransferSettings() {
  const { t } = useTranslation();
  const { settings, updateSettings } = useApp();
  const [folder, setFolder] = useState(settings?.transfer_incoming_dir ?? "");
  const [enabled, setEnabled] = useState(settings?.transfer_watch_enabled ?? true);
  const [interval, setIntervalSeconds] = useState(String(settings?.transfer_scan_interval_seconds ?? 30));
  const [settle, setSettle] = useState(String(settings?.transfer_settle_seconds ?? 120));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setFolder(settings?.transfer_incoming_dir ?? "");
    setEnabled(settings?.transfer_watch_enabled ?? true);
    setIntervalSeconds(String(settings?.transfer_scan_interval_seconds ?? 30));
    setSettle(String(settings?.transfer_settle_seconds ?? 120));
  }, [settings]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaved(false);
    try {
      await updateSettings({
        transfer_incoming_dir: folder.trim(),
        transfer_watch_enabled: enabled,
        transfer_scan_interval_seconds: Number(interval),
        transfer_settle_seconds: Number(settle),
      });
      setSaved(true);
    } catch (err) {
      setError(errorKey(err));
    }
  }

  return (
    <form className="panel settings-panel" onSubmit={submit}>
      <h2 className="panel-title">{t("pages.settings.transfer_title")}</h2>
      <p className="muted">{t("pages.settings.transfer_help")}</p>
      <input className="path-input" value={folder} onChange={(e) => setFolder(e.target.value)} spellCheck={false}
        aria-label={t("pages.settings.transfer_folder")} placeholder={t("pages.settings.transfer_folder")} />
      <label className="check-field">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        {t("pages.settings.transfer_watch")}
      </label>
      <div className="inline-form">
        <label className="field">
          {t("pages.settings.transfer_interval")}
          <input type="number" min={10} max={3600} value={interval} onChange={(e) => setIntervalSeconds(e.target.value)} />
        </label>
        <label className="field">
          {t("pages.settings.transfer_settle")}
          <input type="number" min={0} max={86400} value={settle} onChange={(e) => setSettle(e.target.value)} />
        </label>
      </div>
      <p className="muted small">{t("pages.settings.transfer_protocol")}</p>
      <button type="submit" className="primary-button">{t("common.save")}</button>
      {error && <p className="form-error">{t(error)}</p>}
      {saved && <p className="form-ok">{t("common.saved")}</p>}
    </form>
  );
}

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
      <TransferSettings />
      {version && (
        <p className="muted small version-line">
          THERMALIS · {t("pages.settings.version")} {version}
        </p>
      )}
    </>
  );
}
