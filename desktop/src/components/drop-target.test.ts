import { describe, expect, it } from "vitest";
import { droppedUrls } from "./drop-target";

const data = (types: Record<string, string>) => ({ getData: (type: string) => types[type] ?? "" });

describe("droppedUrls", () => {
  it("reads text/uri-list, skipping comments and duplicates", () => {
    const list = "# from a browser\r\nhttps://e.example/a.iso\r\nhttps://e.example/a.iso\r\nhttps://e.example/b.iso";
    expect(droppedUrls(data({ "text/uri-list": list }))).toEqual(["https://e.example/a.iso", "https://e.example/b.iso"]);
  });

  it("falls back to plain text and rejects what is not a link", () => {
    expect(droppedUrls(data({ "text/plain": "magnet:?xt=urn:btih:abc" }))).toEqual(["magnet:?xt=urn:btih:abc"]);
    expect(droppedUrls(data({ "text/plain": "just words" }))).toEqual([]);
    expect(droppedUrls(data({ "text/uri-list": "file:///etc/passwd" }))).toEqual([]);
  });
});
