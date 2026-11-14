import { describe, expect, it } from "vitest";
import { historyFileName, parseHistory } from "./history";

describe("history file", () => {
  it("names the file by UTC day", () => {
    expect(historyFileName(new Date("2026-11-14T23:30:00Z"))).toBe("sayl-history-2026-11-14.json");
  });

  it("accepts an export and refuses anything else", () => {
    expect(parseHistory('{"format":"sayl-history","version":1,"downloads":[]}')).not.toBeNull();
    expect(parseHistory('{"format":"other","downloads":[]}')).toBeNull();
    expect(parseHistory('{"format":"sayl-history"}')).toBeNull();
    expect(parseHistory("[1]")).toBeNull();
    expect(parseHistory("{")).toBeNull();
  });
});
