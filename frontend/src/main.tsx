import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { useTranslation } from "react-i18next";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { errorKey } from "./api";
import { Layout } from "./components/Layout";
import "./i18n";
import { Home } from "./pages/Home";
import { DataPage } from "./pages/DataCatalog";
import { City3dPage, IntelligencePage, LabDomainPage, LabPage, RiskPage } from "./pages/modules";
import { SettingsPage } from "./pages/SettingsPage";
import { AppProvider, useApp } from "./state";
import "@fontsource-variable/montserrat";
import "./styles.css";

function App() {
  const { t } = useTranslation();
  const { ready, loadError, reload } = useApp();
  if (!ready) return <div className="boot" />;
  if (loadError) {
    return (
      <div className="boot boot-error">
        <p>{t(errorKey(loadError))}</p>
        <button type="button" className="primary-button" onClick={() => void reload()}>
          ↻
        </button>
      </div>
    );
  }
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route element={<Layout />}>
        <Route path="/data" element={<DataPage />} />
        <Route path="/lab" element={<LabPage />} />
        <Route path="/lab/:domain" element={<LabDomainPage />} />
        <Route path="/risk" element={<RiskPage />} />
        <Route path="/city3d" element={<City3dPage />} />
        <Route path="/intelligence" element={<IntelligencePage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AppProvider>
        <App />
      </AppProvider>
    </BrowserRouter>
  </StrictMode>,
);
