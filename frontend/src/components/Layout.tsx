import { useTranslation } from "react-i18next";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { MODULES, moduleForPath } from "../modules";
import { Brand, LanguageToggle, SettingsButton } from "./common";
import { ProjectSelect } from "./projects";

export function Layout() {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const current = moduleForPath(pathname);
  const section = current ? current.label.join(" ") : pathname.startsWith("/settings") ? t("common.settings") : undefined;

  return (
    <div className="app-shell">
      <header className="topbar">
        <Brand section={section} />
        <div className="topbar-actions">
          <ProjectSelect compact />
          <LanguageToggle />
          <SettingsButton />
        </div>
      </header>
      <nav className="sidebar" aria-label="THERMALIS">
        {MODULES.map((m) => (
          <NavLink key={m.key} to={m.path} className="nav-item" style={{ "--accent": m.accent } as React.CSSProperties}>
            <m.icon size={26} strokeWidth={1.5} aria-hidden />
            <span>
              {m.label.map((line) => (
                <span key={line} className="nav-line">
                  {line}
                </span>
              ))}
            </span>
          </NavLink>
        ))}
      </nav>
      <main className="content">
        <Outlet />
      </main>
      <div className="earth-glow" aria-hidden />
    </div>
  );
}

export function PageHeader({ lead, accent, subtitle, actions }: { lead?: string; accent: string; subtitle?: string; actions?: React.ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <h1>
          {lead && <span>{lead} </span>}
          <span className="accent-text">{accent}</span>
        </h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}
