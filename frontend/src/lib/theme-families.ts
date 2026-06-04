export type ThemeSwatchSet = readonly [string, string, string, string];

export type ThemeFamily = {
  id: string;
  label: string;
  description: string;
  light: ThemeSwatchSet;
  dark: ThemeSwatchSet;
};

export const THEME_FAMILIES = [
  {
    id: "indigo",
    label: "Indigo",
    description: "Balanced and familiar, with a softer light surface.",
    light: ["#eef3f8", "#f7f9fc", "#6366f1", "#38bdf8"],
    dark: ["#0f172a", "#1e293b", "#818cf8", "#38bdf8"],
  },
  {
    id: "ocean",
    label: "Ocean",
    description: "Cool blues with a calmer, slightly deeper surface.",
    light: ["#eef6fd", "#f6fbff", "#0284c7", "#14b8a6"],
    dark: ["#0a1220", "#172033", "#38bdf8", "#14b8a6"],
  },
  {
    id: "ember",
    label: "Ember",
    description: "Warm neutrals and a softer, warmer base.",
    light: ["#fef2e8", "#fff8f1", "#f97316", "#f59e0b"],
    dark: ["#1f130f", "#2d1d17", "#fb923c", "#f59e0b"],
  },
  {
    id: "slate",
    label: "Slate",
    description: "Neutral and restrained, with cool gray-blue surfaces.",
    light: ["#eef2f7", "#f8fafc", "#475569", "#94a3b8"],
    dark: ["#111827", "#1e293b", "#cbd5e1", "#94a3b8"],
  },
] as const satisfies readonly ThemeFamily[];

export type ThemeFamilyId = (typeof THEME_FAMILIES)[number]["id"];

export const DEFAULT_THEME_FAMILY: ThemeFamilyId = "indigo";
