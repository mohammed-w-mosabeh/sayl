import { describe, expect, it } from "vitest";
import { asPages, FRESH_SECONDS, pickOffer, type MediaPage } from "./media";

const NOW = 1_000_000;
const page = (url: string, items: MediaPage["items"], age = 0): MediaPage => ({
  page_url: url,
  host: new URL(url).hostname,
  items,
  updated: NOW - age,
});

describe("pickOffer", () => {
  it("prefers the yt-dlp page over manifests on the same page", () => {
    const pages = [
      page("https://v.example/watch?v=1", [
        { kind: "manifest", url: "https://cdn.example/a.m3u8" },
        { kind: "page", url: "https://v.example/watch?v=1", extractor: "Youtube" },
      ]),
    ];
    expect(pickOffer(pages, new Set(), NOW)).toEqual({
      page: "https://v.example/watch?v=1",
      host: "v.example",
      url: "https://v.example/watch?v=1",
      referer: "",
    });
  });

  it("offers a manifest with its page as referer, newest page first", () => {
    const pages = [
      page("https://old.example/", [{ kind: "manifest", url: "https://cdn.example/old.m3u8" }], 60),
      page("https://new.example/", [{ kind: "manifest", url: "https://cdn.example/new.mpd" }]),
    ];
    expect(pickOffer(pages, new Set(), NOW)?.url).toBe("https://cdn.example/new.mpd");
    expect(pickOffer(pages, new Set(), NOW)?.referer).toBe("https://new.example/");
  });

  it("skips dismissed, protected, and stale pages", () => {
    const pages = [
      page("https://a.example/", [{ kind: "manifest", url: "https://cdn.example/a.m3u8" }]),
      page("https://b.example/", [{ kind: "manifest", url: "https://cdn.example/b.mpd", drm: true }]),
      page("https://c.example/", [{ kind: "manifest", url: "https://cdn.example/c.m3u8" }], FRESH_SECONDS + 1),
    ];
    expect(pickOffer(pages, new Set(["https://a.example/"]), NOW)).toBeNull();
  });
});

describe("asPages", () => {
  it("drops anything that is not a media page", () => {
    expect(asPages(null)).toEqual([]);
    expect(asPages([{ page_url: "x" }, { page_url: "y", items: [], host: "", updated: 0 }])).toHaveLength(1);
  });
});
