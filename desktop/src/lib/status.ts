import type { Download, Status } from "@/lib/api/client";
import type { Key } from "@/lib/i18n";
import type { Filter } from "@/lib/prefs";

export type Tone = "primary" | "success" | "warning" | "danger" | "muted";

export function degraded(row: Download): boolean {
  return "quality_degraded" in (row.warnings ?? {});
}

export function statusKey(row: Download): Key {
  if (row.status === "completed" && degraded(row)) return "status.completed_warning";
  return `status.${row.status}`;
}

export function tone(row: Download): Tone {
  switch (row.status) {
    case "active":
    case "probing":
      return "primary";
    case "completed":
      return degraded(row) ? "warning" : "success";
    case "failed":
    case "canceled":
      return "danger";
    case "needs_refresh":
      return "warning";
    default:
      return "muted";
  }
}

const FILTERS: Record<Exclude<Filter, "all">, readonly Status[]> = {
  active: ["active", "probing"],
  queued: ["queued"],
  paused: ["paused"],
  completed: ["completed"],
  failed: ["failed", "canceled"],
  needs_refresh: ["needs_refresh"],
};

export const FILTER_ORDER: Filter[] = [
  "all",
  "active",
  "queued",
  "paused",
  "completed",
  "failed",
  "needs_refresh",
];

export function matches(row: Download, filter: Filter): boolean {
  return filter === "all" || FILTERS[filter].includes(row.status);
}

export function counts(rows: Download[]): Record<Filter, number> {
  const result = Object.fromEntries(FILTER_ORDER.map((filter) => [filter, 0])) as Record<Filter, number>;
  for (const row of rows) {
    for (const filter of FILTER_ORDER) if (matches(row, filter)) result[filter] += 1;
  }
  return result;
}

export function canPause(row: Download): boolean {
  return row.status === "active" || row.status === "queued";
}

/** A captured stream cannot be queued again: it needs a fresh link instead. */
export function canResume(row: Download): boolean {
  if (row.engine === "hybrid") return row.status === "paused";
  return row.status === "paused" || row.status === "failed" || row.status === "canceled";
}

export function title(row: Download): string {
  if (row.filename) return row.filename;
  try {
    const url = new URL(row.url);
    return decodeURIComponent(url.pathname.split("/").filter(Boolean).pop() ?? "") || url.hostname;
  } catch {
    return row.url;
  }
}

export function search(rows: Download[], text: string): Download[] {
  const needle = text.trim().toLocaleLowerCase();
  if (!needle) return rows;
  return rows.filter((row) =>
    [row.filename, row.url, row.page_url].some((field) => field.toLocaleLowerCase().includes(needle)),
  );
}

/** Newest first, as people look for what they just added. */
export function sorted(rows: Download[]): Download[] {
  return [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at));
}
