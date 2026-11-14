import { catalogs, type I18n, type Key } from "@/lib/i18n";

/** The seeded categories carry English names; show them in the app's language. */
export function categoryName(t: I18n["t"], name: string): string {
  const key = `category.${name}`;
  return key in catalogs.en ? t(key as Key) : name;
}
