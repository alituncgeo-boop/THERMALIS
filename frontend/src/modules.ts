import {
  Box,
  ChartColumn,
  CloudLightning,
  Flame,
  Layers,
  Sparkle,
  Sprout,
  Sun,
  Waves,
  Droplets,
  type LucideIcon,
} from "lucide-react";

/** Ürün adları (DATA, HEAT…) marka adıdır; iki dilde de aynı yazılır. */
export interface ModuleDef {
  key: "data" | "analysis_lab" | "risk_engine" | "city3d" | "intelligence";
  label: string[]; // satır satır gösterilen marka adı
  path: string;
  icon: LucideIcon;
  accent: string;
}

export const MODULES: ModuleDef[] = [
  { key: "data", label: ["DATA"], path: "/data", icon: Layers, accent: "var(--c-data)" },
  { key: "analysis_lab", label: ["ANALYSIS", "LAB"], path: "/lab", icon: ChartColumn, accent: "var(--c-lab)" },
  { key: "risk_engine", label: ["RISK", "ENGINE"], path: "/risk", icon: Layers, accent: "var(--c-risk)" },
  { key: "city3d", label: ["3D CITY", "ENGINE"], path: "/city3d", icon: Box, accent: "var(--c-city3d)" },
  { key: "intelligence", label: ["INTELLIGENCE"], path: "/intelligence", icon: Sparkle, accent: "var(--c-intel)" },
];

export interface DomainDef {
  key: "heat" | "drought" | "flood" | "coast" | "fire" | "extremes";
  label: string;
  icon: LucideIcon;
  accent: string;
}

export const DOMAINS: DomainDef[] = [
  { key: "heat", label: "HEAT", icon: Sun, accent: "var(--c-heat)" },
  { key: "drought", label: "DROUGHT", icon: Sprout, accent: "var(--c-drought)" },
  { key: "flood", label: "FLOOD", icon: Droplets, accent: "var(--c-flood)" },
  { key: "coast", label: "COAST", icon: Waves, accent: "var(--c-coast)" },
  { key: "fire", label: "FIRE", icon: Flame, accent: "var(--c-fire)" },
  { key: "extremes", label: "EXTREMES", icon: CloudLightning, accent: "var(--c-extremes)" },
];

export function moduleForPath(pathname: string): ModuleDef | undefined {
  return MODULES.find((m) => pathname === m.path || pathname.startsWith(`${m.path}/`));
}
