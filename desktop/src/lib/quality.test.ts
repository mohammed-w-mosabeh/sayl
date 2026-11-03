import { describe, expect, it } from "vitest";
import { formatForHeight, isProtected, playlistItems, qualities, type Format } from "./quality";

const formats: Format[] = [
  { format_id: "140", vcodec: "none", acodec: "mp4a", abr: 128, filesize: 3_000 },
  { format_id: "251", vcodec: "none", acodec: "opus", abr: 160, filesize: 4_000 },
  { format_id: "137", vcodec: "avc1", acodec: "none", height: 1080, fps: 30, tbr: 4000, filesize: 90_000 },
  { format_id: "248", vcodec: "vp9", acodec: "none", height: 1080, fps: 30, tbr: 3000, filesize: 70_000 },
  { format_id: "22", vcodec: "avc1", acodec: "mp4a", height: 720, tbr: 2000, filesize: 50_000 },
  { format_id: "sb0", vcodec: "none", acodec: "none", ext: "mhtml" },
];

describe("qualities", () => {
  it("offers best, then one option per height, highest first", () => {
    const options = qualities(formats);
    expect(options.map((option) => option.height)).toEqual([null, 1080, 720]);
    expect(options[0]?.format).toBe("");
  });

  it("adds the best audio to a video-only size, but not to a muxed one", () => {
    const [, hd, sd] = qualities(formats);
    expect(hd?.size).toBe(90_000 + 4_000);
    expect(sd?.size).toBe(50_000);
  });

  it("caps by height so yt-dlp and the quality check agree", () => {
    expect(formatForHeight(720)).toBe("bestvideo[height<=720]+bestaudio/best[height<=720]");
    expect(formatForHeight(null)).toBe("");
  });

  it("an audio-only source has only 'best'", () => {
    expect(qualities(formats.slice(0, 2))).toHaveLength(1);
  });
});

describe("isProtected", () => {
  it("is true only when every format needs a licence", () => {
    expect(isProtected([{ has_drm: true }, { has_drm: "maybe" }])).toBe(true);
    expect(isProtected([{ has_drm: true }, {}])).toBe(false);
    expect(isProtected([])).toBe(false);
  });
});

describe("playlistItems", () => {
  it("compresses runs into ranges", () => {
    expect(playlistItems([7, 1, 3, 5, 6])).toBe("1,3,5-7");
    expect(playlistItems([2, 2, 3])).toBe("2-3");
    expect(playlistItems([])).toBe("");
  });
});
