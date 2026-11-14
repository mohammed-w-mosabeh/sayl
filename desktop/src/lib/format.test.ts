import { describe, expect, it } from "vitest";
import { bytes, dateTime, duration, eta, isDownloadUrl, percent, speed } from "./format";
import { makeI18n } from "./i18n";
import arabic from "./i18n/ar.json";

const en = makeI18n("en");
const ar = makeI18n("ar");

describe("bytes", () => {
  it("uses binary units with one decimal below 100", () => {
    expect(bytes(en, 0)).toBe("0 B");
    expect(bytes(en, 1536)).toBe("1.5 KB");
    expect(bytes(en, 4 * 1024 ** 3)).toBe("4.0 GB");
    expect(bytes(en, 150 * 1024 ** 2)).toBe("150 MB");
    expect(bytes(en, null)).toBe("—");
  });

  it("keeps Latin digits in Arabic", () => {
    expect(bytes(ar, 1536)).toBe(`1.5 ${arabic["units.KB"]}`);
    expect(speed(ar, 2 * 1024 ** 2)).toBe(arabic["units.per_second"].replace("{size}", `2.0 ${arabic["units.MB"]}`));
  });
});

describe("time", () => {
  it("formats durations", () => {
    expect(duration(en, 42)).toBe("42s");
    expect(duration(en, 125)).toBe("2m 5s");
    expect(duration(en, 3 * 3600 + 120)).toBe("3h 2m");
    expect(duration(en, null)).toBe("—");
  });

  it("estimates time left only when it can be known", () => {
    expect(eta(50, 100, 10)).toBe(5);
    expect(eta(50, 0, 10)).toBeNull();
    expect(eta(50, 100, 0)).toBeNull();
    expect(percent(1, 4)).toBe(25);
    expect(percent(1, 0)).toBeNull();
  });

  it("reads the daemon's naive UTC timestamps as UTC", () => {
    expect(dateTime(en, "2026-11-03T10:00:00")).toBe(dateTime(en, "2026-11-03T10:00:00Z"));
  });
});

describe("isDownloadUrl", () => {
  it.each([
    ["https://example.com/a.iso", true],
    ["ftp://mirror.example/a", true],
    ["magnet:?xt=urn:btih:abc", true],
    ["  https://example.com/a  ", true],
    ["javascript:alert(1)", false],
    ["file:///etc/passwd", false],
    ["magnet:", false],
    ["two words", false],
    ["https://a.example/x https://b.example/y", false],
    ["", false],
  ])("%s → %s", (value, expected) => {
    expect(isDownloadUrl(value)).toBe(expected);
  });
});
