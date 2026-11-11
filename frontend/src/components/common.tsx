import { Languages, Settings as SettingsIcon, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import { useApp } from "../state";

export function Brand({ section }: { section?: string }) {
  const { t } = useTranslation();
  return (
    <Link to="/" className="brand" aria-label="THERMALIS">
      <img src="/logo-emblem.png" alt="" className="brand-logo" />
      <span className="brand-name">THERMALIS</span>
      <span className="brand-divider" />
      <span className={section ? "brand-section" : "brand-tagline"}>{section ?? t("app.tagline")}</span>
    </Link>
  );
}

export function LanguageToggle() {
  const { t } = useTranslation();
  const { settings, setLanguage } = useApp();
  const current = settings?.language ?? "tr";
  return (
    <div className="lang-toggle" role="group" aria-label={t("common.language")}>
      <Languages size={16} aria-hidden />
      {(["tr", "en"] as const).map((lang) => (
        <button
          key={lang}
          type="button"
          className={lang === current ? "active" : ""}
          aria-pressed={lang === current}
          onClick={() => void setLanguage(lang)}
        >
          {lang.toUpperCase()}
        </button>
      ))}
    </div>
  );
}

export function SettingsButton() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <button type="button" className="icon-button" title={t("common.settings")} aria-label={t("common.settings")} onClick={() => navigate("/settings")}>
      <SettingsIcon size={18} />
    </button>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  body,
  accent,
  children,
}: {
  icon: LucideIcon;
  title: string;
  body: string;
  accent?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state" style={accent ? ({ "--accent": accent } as React.CSSProperties) : undefined}>
      <div className="empty-icon">
        <Icon size={28} />
      </div>
      <h3>{title}</h3>
      <p>{body}</p>
      {children}
    </div>
  );
}

export function StatusPill({ label }: { label: string }) {
  return <span className="status-pill">{label}</span>;
}
