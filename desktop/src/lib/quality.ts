/** Builds the quality picker from a yt-dlp probe. The daemon trims formats to these fields. */

export interface Format {
  format_id?: string;
  format_note?: string;
  ext?: string;
  protocol?: string;
  width?: number;
  height?: number;
  fps?: number;
  vcodec?: string;
  acodec?: string;
  abr?: number;
  tbr?: number;
  filesize?: number;
  filesize_approx?: number;
  dynamic_range?: string;
  has_drm?: boolean | string;
}

export interface Quality {
  /** Null means "best available": the daemon's default format. */
  height: number | null;
  fps: number | null;
  size: number | null;
  format: string;
}

const hasVideo = (format: Format) => !!format.vcodec && format.vcodec !== "none";
const hasAudio = (format: Format) => !!format.acodec && format.acodec !== "none";
const sizeOf = (format: Format) => format.filesize ?? format.filesize_approx ?? null;

/**
 * A height cap rather than a format id: yt-dlp then picks the best stream at or below it, and
 * the daemon's quality check counts a cap against the best height the source actually has.
 */
export function formatForHeight(height: number | null): string {
  return height === null ? "" : `bestvideo[height<=${height}]+bestaudio/best[height<=${height}]`;
}

function bestAudio(formats: Format[]): Format | undefined {
  return formats
    .filter((format) => hasAudio(format) && !hasVideo(format))
    .sort((a, b) => (b.abr ?? b.tbr ?? 0) - (a.abr ?? a.tbr ?? 0))[0];
}

export function qualities(formats: Format[]): Quality[] {
  const video = formats.filter((format) => hasVideo(format) && format.height);
  const audio = bestAudio(formats);
  const byHeight = new Map<number, Format>();
  for (const format of video) {
    const height = format.height as number;
    const current = byHeight.get(height);
    if (!current || (format.tbr ?? 0) > (current.tbr ?? 0)) byHeight.set(height, format);
  }
  const heights = [...byHeight.keys()].sort((a, b) => b - a);
  const options = heights.map((height): Quality => {
    const best = byHeight.get(height) as Format;
    const videoSize = sizeOf(best);
    const audioSize = hasAudio(best) || !audio ? 0 : sizeOf(audio);
    return {
      height,
      fps: best.fps ?? null,
      size: videoSize !== null && audioSize !== null ? videoSize + audioSize : null,
      format: formatForHeight(height),
    };
  });
  return [{ height: null, fps: null, size: options[0]?.size ?? null, format: "" }, ...options];
}

/** Every stream needs a licence: Sayl never circumvents DRM. */
export function isProtected(formats: Format[]): boolean {
  return formats.length > 0 && formats.every((format) => !!format.has_drm);
}

/** Playlist indices (1-based) as yt-dlp's `playlist_items`, e.g. "1,3,5-7". */
export function playlistItems(indices: Iterable<number>): string {
  const sorted = [...new Set(indices)].filter((n) => n > 0).sort((a, b) => a - b);
  const parts: string[] = [];
  let start: number | undefined;
  let end: number | undefined;
  for (const index of sorted) {
    if (start !== undefined && end !== undefined && index === end + 1) {
      end = index;
      continue;
    }
    if (start !== undefined && end !== undefined) parts.push(start === end ? `${start}` : `${start}-${end}`);
    start = index;
    end = index;
  }
  if (start !== undefined && end !== undefined) parts.push(start === end ? `${start}` : `${start}-${end}`);
  return parts.join(",");
}
