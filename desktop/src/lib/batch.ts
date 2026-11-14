/** Matches a range the daemon expands: [1-10], [001-100], [a-z], with an optional :step. */
const RANGE = /\[(?:\d+-\d+|[a-zA-Z]-[a-zA-Z])(?::\d+)?\]/;

/** Lines become plain links or patterns; blank lines and # comments are dropped. */
export function splitBatch(text: string): { urls: string[]; patterns: string[] } {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  return {
    urls: lines.filter((line) => !RANGE.test(line)),
    patterns: lines.filter((line) => RANGE.test(line)),
  };
}

/** Megabytes typed by the person, as bytes; empty or invalid is 0 (no limit). */
export function megabytes(text: string): number {
  const value = Number(text.trim().replace(",", "."));
  return Number.isFinite(value) && value > 0 ? Math.round(value * 1024 * 1024) : 0;
}

export function extensionList(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\s,،]+/)
        .map((item) => item.replace(/^\./, "").toLowerCase())
        .filter(Boolean),
    ),
  ];
}
