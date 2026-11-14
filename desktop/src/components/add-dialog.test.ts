import { describe, expect, it } from "vitest";
import type { Probe } from "@/lib/api/client";
import { buildRequest, type Choices } from "./add-dialog";
import { ignoreList } from "./settings-view";

const choices: Choices = {
  filename: "file.bin",
  quality: 1,
  audioOnly: false,
  subtitles: [],
  items: new Set([1, 2, 3]),
  destDir: "/d",
  limit: "512",
  paused: true,
};

const video: Probe = {
  url: "https://www.youtube.com/playlist?list=x",
  filename: "List",
  total: null,
  resumable: true,
  extractor: "youtube:tab",
  formats: [{ format_id: "137", vcodec: "avc1", acodec: "none", height: 1080 }],
  entries: [{ index: 1 }, { index: 2 }, { index: 3 }, { index: 4 }],
  subtitles: ["ar", "en"],
  duration: null,
  thumbnail: "",
  engine: "ytdlp",
};

describe("buildRequest", () => {
  it("sends a plain file with its name, folder, and limit in bytes", () => {
    const body = buildRequest(" https://e.example/f ", { ...video, engine: "aria2", formats: [], entries: [] }, choices);
    expect(body).toEqual({
      url: "https://e.example/f",
      filename: "file.bin",
      dest_dir: "/d",
      start_paused: true,
      speed_limit: 512 * 1024,
    });
  });

  it("sends a height cap, subtitles, and only the chosen playlist items", () => {
    const body = buildRequest(video.url, video, { ...choices, subtitles: ["ar"] });
    expect(body).toMatchObject({
      filename: "",
      audio_only: false,
      format_id: "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
      subtitles: ["ar"],
      playlist_items: "1-3",
    });
  });

  it("leaves the format to the daemon for audio only, and omits a full playlist", () => {
    const body = buildRequest(video.url, video, { ...choices, audioOnly: true, items: new Set([1, 2, 3, 4]) });
    expect(body.audio_only).toBe(true);
    expect(body.format_id).toBeUndefined();
    expect(body.playlist_items).toBeUndefined();
  });
});

describe("ignoreList", () => {
  it("splits lines and commas, lowercases, and drops blanks and repeats", () => {
    expect(ignoreList("Bank.example\n\n*.pdf, bank.example\u060Cshop.example ")).toEqual([
      "bank.example",
      "*.pdf",
      "shop.example",
    ]);
  });
});
