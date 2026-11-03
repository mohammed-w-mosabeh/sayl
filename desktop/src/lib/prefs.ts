"use client";

import { create } from "zustand";
import type { Language } from "@/lib/i18n";

export type Theme = "system" | "light" | "dark";
export type Filter =
  | "all"
  | "active"
  | "queued"
  | "paused"
  | "completed"
  | "failed"
  | "needs_refresh";

export interface Prefs {
  language: Language;
  theme: Theme;
  notifications: boolean;
}

const KEY = "sayl.prefs";
export const DEFAULT_PREFS: Prefs = { language: "en", theme: "system", notifications: true };

/**
 * Appearance is cached here so the first frame is already in the right language and theme.
 * The daemon's `ui` setting is the durable copy; see `useSyncPrefs`.
 */
export function readCachedPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return normalizePrefs(JSON.parse(raw));
  } catch {
    // Storage may be unavailable; the defaults still render.
  }
  if (typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("ar")) {
    return { ...DEFAULT_PREFS, language: "ar" };
  }
  return DEFAULT_PREFS;
}

export function hasCachedPrefs(): boolean {
  try {
    return localStorage.getItem(KEY) !== null;
  } catch {
    return false;
  }
}

export function normalizePrefs(value: unknown): Prefs {
  const source = (value && typeof value === "object" ? value : {}) as Partial<Prefs>;
  return {
    language: source.language === "ar" ? "ar" : "en",
    theme: source.theme === "light" || source.theme === "dark" ? source.theme : "system",
    notifications: source.notifications !== false,
  };
}

function cache(prefs: Prefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    // Not fatal: the daemon keeps the durable copy.
  }
}

interface UiState {
  prefs: Prefs;
  /** Whether this webview had its own copy at start; if not, the daemon's copy wins. */
  prefsCached: boolean;
  prefsLoaded: boolean;
  bootPrefs: () => void;
  setPrefs: (prefs: Partial<Prefs>) => void;
  filter: Filter;
  setFilter: (filter: Filter) => void;
  category: string | null;
  setCategory: (category: string | null) => void;
  search: string;
  setSearch: (search: string) => void;
  selected: string | null;
  select: (id: string | null) => void;
  adding: { open: boolean; url: string };
  openAdd: (url?: string) => void;
  closeAdd: () => void;
  live: boolean;
  setLive: (live: boolean) => void;
}

export const useUi = create<UiState>((set) => ({
  prefs: DEFAULT_PREFS,
  prefsCached: false,
  prefsLoaded: false,
  bootPrefs: () =>
    set({ prefs: readCachedPrefs(), prefsCached: hasCachedPrefs(), prefsLoaded: true }),
  setPrefs: (changes) =>
    set((state) => {
      const prefs = normalizePrefs({ ...state.prefs, ...changes });
      cache(prefs);
      return { prefs };
    }),
  filter: "all",
  setFilter: (filter) => set({ filter, category: null }),
  category: null,
  setCategory: (category) => set({ category, filter: "all" }),
  search: "",
  setSearch: (search) => set({ search }),
  selected: null,
  select: (selected) => set({ selected }),
  adding: { open: false, url: "" },
  openAdd: (url = "") => set({ adding: { open: true, url } }),
  closeAdd: () => set({ adding: { open: false, url: "" } }),
  live: false,
  setLive: (live) => set({ live }),
}));
