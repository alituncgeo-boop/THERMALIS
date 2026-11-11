import { ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { Brand, LanguageToggle, SettingsButton } from "../components/common";
import { CreateProjectButton, ProjectSelect } from "../components/projects";
import { MODULES } from "../modules";

export function Home() {
  const { t } = useTranslation();
  return (
    <div className="home">
      <header className="home-header">
        <Brand />
        <div className="topbar-actions">
          <LanguageToggle />
          <SettingsButton />
        </div>
      </header>

      <section className="hero">
        <h1>
          {t("home.title_lead")} <span className="gradient-text">{t("home.title_accent")}</span>
        </h1>
        <p className="hero-subtitle">{t("home.subtitle")}</p>
        <div className="hero-rule" />
      </section>

      <section className="module-grid">
        {MODULES.map((m) => (
          <Link key={m.key} to={m.path} className="module-card" style={{ "--accent": m.accent } as React.CSSProperties} title={t(`modules.${m.key}`)}>
            <m.icon size={52} strokeWidth={1.2} aria-hidden />
            <span className="module-name">
              {m.label.map((line) => (
                <span key={line}>{line}</span>
              ))}
            </span>
            <span className="module-desc">{t(`modules.${m.key}`)}</span>
            <span className="module-go" aria-hidden>
              <ArrowRight size={18} />
            </span>
          </Link>
        ))}
      </section>

      <section className="city-bar">
        <ProjectSelect />
        <span className="city-bar-divider" />
        <CreateProjectButton />
      </section>

      <div className="earth" aria-hidden />
    </div>
  );
}
