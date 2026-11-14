/** Helpers for the history file. The daemon validates it; this only catches the obvious. */

export function historyFileName(now: Date): string {
  const day = now.toISOString().slice(0, 10);
  return `sayl-history-${day}.json`;
}

/** The parsed file if it looks like a Sayl history export, else null. */
export function parseHistory(text: string): Record<string, unknown> | null {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const document = value as Record<string, unknown>;
  return document.format === "sayl-history" && Array.isArray(document.downloads) ? document : null;
}

/** Outside the desktop app: hand the export to the browser as a file. */
export function downloadJson(document: unknown, name: string): void {
  const blob = new Blob([JSON.stringify(document, null, 1)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = Object.assign(window.document.createElement("a"), { href: url, download: name });
  link.click();
  URL.revokeObjectURL(url);
}
