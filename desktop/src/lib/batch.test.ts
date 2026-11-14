import { describe, expect, it } from "vitest";
import { extensionList, megabytes, splitBatch } from "./batch";

describe("batch", () => {
  it("splits links from patterns", () => {
    const text = "# list\nhttps://h/a.zip\n\nhttps://h/f[001-010].jpg\nhttp://[::1]/x\nhttps://h/[a-c]\n";
    expect(splitBatch(text)).toEqual({
      urls: ["https://h/a.zip", "http://[::1]/x"],
      patterns: ["https://h/f[001-010].jpg", "https://h/[a-c]"],
    });
  });

  it("reads sizes and types", () => {
    expect(megabytes("1.5")).toBe(1572864);
    expect(megabytes("1,5")).toBe(1572864);
    expect(megabytes("")).toBe(0);
    expect(megabytes("x")).toBe(0);
    expect(extensionList(".PDF, zip،zip iso")).toEqual(["pdf", "zip", "iso"]);
  });
});
