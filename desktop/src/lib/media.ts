/** What the floating video bar offers, from sayl-capture's media reports (`GET /capture/media`). */

export interface MediaItem {
  kind: string; // "page" (yt-dlp knows the site) or "manifest" (.m3u8 / .mpd seen in traffic).
  url: string;
  drm?: boolean;
  extractor?: string;
}

export interface MediaPage {
  page_url: string;
  host: string;
  items: MediaItem[];
  updated: number; // Seconds since the epoch.
}

export interface Offer {
  page: string;
  host: string;
  /** What to probe and download: the page for a yt-dlp site, otherwise the manifest. */
  url: string;
  referer: string;
}

/** Reports older than this belong to a video the person has moved on from. */
export const FRESH_SECONDS = 10 * 60;

export function asPages(value: unknown): MediaPage[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (page): page is MediaPage =>
      !!page &&
      typeof page === "object" &&
      typeof (page as MediaPage).page_url === "string" &&
      Array.isArray((page as MediaPage).items),
  );
}

/**
 * The newest page worth offering. A yt-dlp site wins over its own manifests, because yt-dlp
 * lists every quality. Protected streams are never offered: Sayl does not circumvent DRM.
 */
export function pickOffer(pages: MediaPage[], dismissed: ReadonlySet<string>, now: number): Offer | null {
  const newest = [...pages].sort((a, b) => b.updated - a.updated);
  for (const page of newest) {
    if (now - page.updated > FRESH_SECONDS) break;
    if (dismissed.has(page.page_url)) continue;
    const site = page.items.find((item) => item.kind === "page" && item.extractor);
    if (site) return { page: page.page_url, host: page.host, url: page.page_url, referer: "" };
    if (page.items.some((item) => item.drm)) continue;
    const manifest = page.items.find((item) => item.kind === "manifest");
    if (manifest) return { page: page.page_url, host: page.host, url: manifest.url, referer: page.page_url };
  }
  return null;
}
