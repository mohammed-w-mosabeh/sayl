"use client";

import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";
import ar from "./ar.json";
import en from "./en.json";

export type Key = keyof typeof en;
export type Language = "en" | "ar";
export type Vars = Record<string, string | number>;

export const catalogs: Record<Language, Record<Key, string>> = { en, ar };
export const languages: Record<Language, string> = { en: "English", ar: "العربية" };

export function direction(language: Language): "ltr" | "rtl" {
  return language === "ar" ? "rtl" : "ltr";
}

/** Latin digits in both languages: sizes and speeds then read the same as file names and URLs. */
export function locale(language: Language): string {
  return language === "ar" ? "ar-u-nu-latn" : "en";
}

export function translate(language: Language, key: Key, vars: Vars = {}): string {
  const template = catalogs[language][key] ?? catalogs.en[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}

export interface I18n {
  language: Language;
  dir: "ltr" | "rtl";
  locale: string;
  t: (key: Key, vars?: Vars) => string;
}

export function makeI18n(language: Language): I18n {
  return {
    language,
    dir: direction(language),
    locale: locale(language),
    t: (key, vars) => translate(language, key, vars),
  };
}

const I18nContext = createContext<I18n>(makeI18n("en"));

export function I18nProvider({ language, children }: { language: Language; children: ReactNode }) {
  const value = useMemo(() => makeI18n(language), [language]);
  useEffect(() => {
    document.documentElement.lang = language;
    document.documentElement.dir = value.dir;
  }, [language, value.dir]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  return useContext(I18nContext);
}
