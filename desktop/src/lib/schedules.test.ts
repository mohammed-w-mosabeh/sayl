import { describe, expect, it } from "vitest";
import { cronFor, parseCron } from "./schedules";

describe("schedules", () => {
  it("builds daily crons", () => {
    expect(cronFor("02:30", "every")).toBe("30 2 * * *");
    expect(cronFor("23:05", "mon_fri")).toBe("5 23 * * 1-5");
    expect(cronFor("07:00", "sun_thu")).toBe("0 7 * * 0-4");
    expect(cronFor("", "every")).toBe("");
    expect(cronFor("x", "custom", " */5 * * * * ")).toBe("*/5 * * * *");
  });

  it("reads them back, or calls them custom", () => {
    expect(parseCron("5 23 * * 1-5")).toEqual({ time: "23:05", days: "mon_fri" });
    expect(parseCron("0 2 1 * *")).toEqual({ time: "", days: "custom" });
  });
});
