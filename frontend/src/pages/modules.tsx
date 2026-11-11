import { ArrowLeft, ArrowRight, Box, Layers, MessageSquare, Plus, Send, Sparkle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, Navigate, useParams } from "react-router-dom";
import { EmptyState, StatusPill } from "../components/common";
import { PageHeader } from "../components/Layout";
import { RequireProject } from "../components/projects";
import { DOMAINS } from "../modules";
import { useApp } from "../state";

function useProjectName(): string {
  return useApp().activeProject?.name ?? "";
}

export function DataPage() {
  const { t } = useTranslation();
  const project = useProjectName();
  return (
    <RequireProject>
      <PageHeader
        lead="THERMALIS"
        accent="DATA"
        subtitle={t("pages.data.subtitle", { project })}
        actions={
          <button type="button" className="primary-button" disabled title={t("common.next_step")}>
            <Plus size={18} aria-hidden />
            {t("pages.data.add_dataset")}
          </button>
        }
      />
      <div className="panel">
        <EmptyState icon={Layers} accent="var(--c-data)" title={t("pages.data.empty_title")} body={t("pages.data.empty_body")} />
      </div>
    </RequireProject>
  );
}

export function LabPage() {
  const { t } = useTranslation();
  const project = useProjectName();
  return (
    <RequireProject>
      <PageHeader lead="THERMALIS" accent="ANALYSIS LAB" subtitle={t("pages.lab.subtitle", { project })} />
      <div className="domain-grid">
        {DOMAINS.map((d) => (
          <Link key={d.key} to={`/lab/${d.key}`} className="domain-card" style={{ "--accent": d.accent } as React.CSSProperties}>
            <d.icon size={44} strokeWidth={1.3} aria-hidden />
            <span className="domain-name">{d.label}</span>
            <span className="domain-desc">{t(`domains.${d.key}`)}</span>
            <span className="domain-footer">
              <StatusPill label={t("status.not_started")} />
              <span className="open-link">
                {t("common.open_workspace")} <ArrowRight size={16} aria-hidden />
              </span>
            </span>
          </Link>
        ))}
      </div>
    </RequireProject>
  );
}

export function LabDomainPage() {
  const { t } = useTranslation();
  const { domain } = useParams();
  const def = DOMAINS.find((d) => d.key === domain);
  if (!def) return <Navigate to="/lab" replace />;
  return (
    <RequireProject>
      <Link to="/lab" className="back-link">
        <ArrowLeft size={16} aria-hidden /> ANALYSIS LAB
      </Link>
      <PageHeader accent={def.label} subtitle={t(`domains.${def.key}`)} />
      <div className="panel" style={{ "--accent": def.accent } as React.CSSProperties}>
        <EmptyState icon={def.icon} accent={def.accent} title={t("pages.lab.domain_empty_title")} body={t("pages.lab.domain_empty_body")} />
      </div>
    </RequireProject>
  );
}

export function RiskPage() {
  const { t } = useTranslation();
  return (
    <RequireProject>
      <PageHeader lead="THERMALIS" accent="RISK ENGINE" />
      <div className="panel">
        <h2 className="panel-title">{t("pages.risk.readiness_title")}</h2>
        <p className="muted">{t("pages.risk.readiness_subtitle")}</p>
        <div className="readiness-grid">
          {DOMAINS.map((d) => (
            <div key={d.key} className="readiness-card" style={{ "--accent": d.accent } as React.CSSProperties}>
              <span className="readiness-head">
                <d.icon size={22} aria-hidden /> {d.label}
              </span>
              <StatusPill label={t("status.no_results")} />
            </div>
          ))}
        </div>
        <p className="muted small">{t("pages.risk.readiness_note")}</p>
      </div>
      <div className="panel">
        <EmptyState icon={Layers} accent="var(--c-risk)" title={t("pages.risk.assessment_empty_title")} body={t("pages.risk.assessment_empty_body")} />
      </div>
    </RequireProject>
  );
}

export function City3dPage() {
  const { t } = useTranslation();
  return (
    <RequireProject>
      <PageHeader lead="THERMALIS" accent="3D CITY ENGINE" />
      <div className="panel panel-tall">
        <EmptyState icon={Box} accent="var(--c-city3d)" title={t("pages.city3d.empty_title")} body={t("pages.city3d.empty_body")} />
      </div>
    </RequireProject>
  );
}

export function IntelligencePage() {
  const { t } = useTranslation();
  return (
    <RequireProject>
      <PageHeader lead="THERMALIS" accent="INTELLIGENCE" />
      <div className="panel panel-tall">
        <EmptyState icon={Sparkle} accent="var(--c-intel)" title={t("pages.intelligence.empty_title")} body={t("pages.intelligence.empty_body")} />
      </div>
      <div className="ask-bar" aria-disabled="true">
        <MessageSquare size={20} aria-hidden />
        <input disabled placeholder={t("pages.intelligence.input_placeholder")} aria-label={t("pages.intelligence.input_placeholder")} />
        <button type="button" className="send-button" disabled title={t("common.next_step")} aria-label={t("common.next_step")}>
          <Send size={18} />
        </button>
      </div>
    </RequireProject>
  );
}
