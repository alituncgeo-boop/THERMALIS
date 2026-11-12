import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type Project, type Settings } from "./api";
import { applyLanguage, type Language } from "./i18n";

interface AppState {
  ready: boolean;
  loadError: unknown;
  settings: Settings | null;
  projects: Project[];
  activeProject: Project | null;
  version: string | null;
  setLanguage: (language: Language) => Promise<void>;
  selectProject: (projectId: string) => Promise<void>;
  createProject: (name: string, description: string) => Promise<Project>;
  setDataRoot: (path: string) => Promise<void>;
  updateSettings: (update: Partial<Omit<Settings, "data_root_is_default">>) => Promise<void>;
  reload: () => Promise<void>;
}

const AppContext = createContext<AppState | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [version, setVersion] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [s, p, h] = await Promise.all([api.settings(), api.projects(), api.health()]);
      applyLanguage(s.language);
      setSettings(s);
      setProjects(p);
      setVersion(h.version);
      setLoadError(null);
    } catch (error) {
      setLoadError(error);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const value = useMemo<AppState>(() => {
    const activeProject = projects.find((p) => p.id === settings?.active_project_id) ?? null;
    return {
      ready,
      loadError,
      settings,
      projects,
      activeProject,
      version,
      async setLanguage(language) {
        applyLanguage(language);
        setSettings(await api.updateSettings({ language }));
      },
      async selectProject(projectId) {
        setSettings(await api.updateSettings({ active_project_id: projectId }));
      },
      async createProject(name, description) {
        const project = await api.createProject(name, description);
        await reload();
        return project;
      },
      async setDataRoot(path) {
        await api.updateSettings({ data_root: path });
        await reload();
      },
      async updateSettings(update) {
        setSettings(await api.updateSettings(update));
      },
      reload,
    };
  }, [ready, loadError, settings, projects, version, reload]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppState {
  const context = useContext(AppContext);
  if (!context) throw new Error("useApp must be used inside AppProvider");
  return context;
}
