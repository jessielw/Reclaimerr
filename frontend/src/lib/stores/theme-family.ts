import { writable } from "svelte/store";

import {
  DEFAULT_THEME_FAMILY,
  THEME_FAMILIES,
  type ThemeFamilyId,
} from "$lib/theme-families";

export const THEME_FAMILY_STORAGE_KEY = "reclaimerr-theme-family";

const VALID_THEME_FAMILIES = new Set<ThemeFamilyId>(
  THEME_FAMILIES.map((preset) => preset.id as ThemeFamilyId),
);

const hasStorage = typeof window !== "undefined";

function normalizeThemeFamily(value: string | null | undefined): ThemeFamilyId {
  if (value && VALID_THEME_FAMILIES.has(value as ThemeFamilyId)) {
    return value as ThemeFamilyId;
  }

  return DEFAULT_THEME_FAMILY;
}

function readThemeFamily(): ThemeFamilyId {
  if (!hasStorage) return DEFAULT_THEME_FAMILY;

  try {
    const storedTheme = window.localStorage.getItem(THEME_FAMILY_STORAGE_KEY);
    return normalizeThemeFamily(storedTheme);
  } catch {
    return DEFAULT_THEME_FAMILY;
  }
}

const initialThemeFamily = readThemeFamily();

export const themeFamily = writable<ThemeFamilyId>(initialThemeFamily);

if (hasStorage) {
  themeFamily.subscribe((value) => {
    try {
      window.localStorage.setItem(
        THEME_FAMILY_STORAGE_KEY,
        normalizeThemeFamily(value),
      );
    } catch {
      // ignore storage failures; the theme still persists for this session
    }
  });
}

export function setThemeFamily(value: ThemeFamilyId) {
  themeFamily.set(normalizeThemeFamily(value));
}
