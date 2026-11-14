import { describe, expect, it } from "vitest";
import { download } from "@/test/fixtures";
import { NETWORK_LOST, canPause, canResume, counts, matches, search, sorted, statusKey, title } from "./status";

describe("status", () => {
  it("marks a degraded completion as a warning", () => {
    expect(statusKey(download({ status: "completed" }))).toBe("status.completed");
    expect(statusKey(download({ status: "completed", warnings: { quality_degraded: {} } }))).toBe(
      "status.completed_warning",
    );
  });

  it("filters and counts", () => {
    const rows = [
      download({ id: "a", status: "probing" }),
      download({ id: "b", status: "failed" }),
      download({ id: "c", status: "canceled" }),
    ];
    expect(matches(rows[0]!, "active")).toBe(true);
    expect(counts(rows)).toMatchObject({ all: 3, active: 1, failed: 2, completed: 0 });
  });

  it("never queues a captured stream again: it needs a fresh link", () => {
    expect(canResume(download({ engine: "hybrid", status: "failed" }))).toBe(false);
    expect(canResume(download({ engine: "hybrid", status: "paused" }))).toBe(true);
    expect(canResume(download({ engine: "hybrid", status: "needs_refresh", error: NETWORK_LOST }))).toBe(true);
    expect(canResume(download({ engine: "hybrid", status: "needs_refresh", error: "link expired" }))).toBe(false);
    expect(canResume(download({ engine: "aria2", status: "failed" }))).toBe(true);
    expect(canPause(download({ status: "needs_refresh" }))).toBe(false);
  });

  it("titles, searches, and sorts newest first", () => {
    expect(title(download({ filename: "", url: "https://h.example/a/b%20c.zip?x=1" }))).toBe("b c.zip");
    expect(title(download({ filename: "", url: "https://h.example/" }))).toBe("h.example");
    const rows = [download({ id: "old", created_at: "2026-01-01T00:00:00" }), download({ id: "new", filename: "Movie.mkv", created_at: "2026-02-01T00:00:00" })];
    expect(sorted(rows).map((row) => row.id)).toEqual(["new", "old"]);
    expect(search(rows, "movie").map((row) => row.id)).toEqual(["new"]);
  });
});
