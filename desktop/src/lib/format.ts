import type { I18n } from "@/lib/i18n";

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** Binary units, as the CLI and file managers show them. */
export function bytes(i18n: I18n, value: number | null | undefined): string {
  if (value === null || value === undefined || value < 0) return i18n.t("time.unknown");
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || size >= 100 ? 0 : 1;
  const number = new Intl.NumberFormat(i18n.locale, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(size);
  return `${number} ${i18n.t(`units.${UNITS[unit] ?? "B"}`)}`;
}

export function speed(i18n: I18n, value: number): string {
  return value > 0 ? i18n.t("units.per_second", { size: bytes(i18n, value) }) : "";
}

export function duration(i18n: I18n, seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0 || !Number.isFinite(seconds)) return i18n.t("time.unknown");
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return i18n.t("time.hours", { h, m });
  if (m > 0) return i18n.t("time.minutes", { m, s });
  return i18n.t("time.seconds", { n: s });
}

/** Seconds left at the current speed, or null when it cannot be known. */
export function eta(done: number, total: number, rate: number): number | null {
  if (total <= 0 || rate <= 0 || done >= total) return null;
  return (total - done) / rate;
}

export function percent(done: number, total: number): number | null {
  if (total <= 0) return null;
  return Math.min(100, Math.max(0, (done / total) * 100));
}

export function dateTime(i18n: I18n, value: string | null | undefined): string {
  if (!value) return i18n.t("time.unknown");
  // The daemon stores UTC; naive timestamps from SQLite carry no zone.
  const parsed = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(i18n.locale, { dateStyle: "medium", timeStyle: "short" }).format(
    parsed,
  );
}

export const ACCEPTED_SCHEMES = ["http:", "https:", "ftp:", "magnet:"] as const;

/** The schemes the daemon accepts from people, as the clipboard watcher will. */
export function isDownloadUrl(text: string): boolean {
  const value = text.trim();
  if (!value || /\s/.test(value)) return false;
  try {
    const url = new URL(value);
    if (url.protocol === "magnet:") return url.search.length > 1;
    return (ACCEPTED_SCHEMES as readonly string[]).includes(url.protocol) && url.hostname !== "";
  } catch {
    return false;
  }
}
