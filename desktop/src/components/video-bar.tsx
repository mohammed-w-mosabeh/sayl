"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Download, LoaderCircle, Play, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { DOWNLOADS, MEDIA } from "@/lib/events";
import { bytes } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { asPages, pickOffer } from "@/lib/media";
import { setVideoBar } from "@/lib/native";
import { useUi } from "@/lib/prefs";
import { isProtected, qualities, type Format } from "@/lib/quality";

/**
 * The floating video grabber. Its window stays hidden until sayl-capture reports a video the
 * person has not dismissed; the bar then lists the qualities yt-dlp finds for it.
 */
export function VideoBar() {
  const i18n = useI18n();
  const { t } = i18n;
  const api = useApi();
  const client = useQueryClient();
  const enabled = useUi((state) => state.prefs.videoBar);
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set());
  const [choice, setChoice] = useState(0);

  useEffect(() => {
    document.documentElement.classList.add("bar-window");
  }, []);

  const media = useQuery({ queryKey: MEDIA, queryFn: api.media, refetchInterval: 60_000 });
  const offer = useMemo(
    () => (enabled ? pickOffer(asPages(media.data), dismissed, media.dataUpdatedAt / 1000) : null),
    [dismissed, enabled, media.data, media.dataUpdatedAt],
  );

  const probe = useQuery({
    queryKey: ["probe", offer?.url, offer?.referer],
    queryFn: () => api.probe(offer?.url ?? "", offer?.referer ? { Referer: offer.referer } : {}),
    enabled: !!offer,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const formats = useMemo(() => (probe.data?.formats ?? []) as Format[], [probe.data]);
  const options = useMemo(() => qualities(formats), [formats]);
  const locked = isProtected(formats);

  const dismiss = () => {
    if (offer) setDismissed((current) => new Set(current).add(offer.page));
    setChoice(0);
  };

  const add = useMutation({
    mutationFn: () =>
      api.add({
        url: offer?.url ?? "",
        referer: offer?.referer || undefined,
        format_id: options[choice]?.format ?? "",
        page_url: offer?.page,
      }),
    onSuccess: () => void client.invalidateQueries({ queryKey: DOWNLOADS }),
  });

  // After an add, say so briefly and then put the bar away.
  useEffect(() => {
    if (!add.isSuccess) return;
    const timer = setTimeout(() => {
      add.reset();
      dismiss();
    }, 1500);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [add.isSuccess]);

  const visible = !!offer && !locked && !probe.isError;
  useEffect(() => {
    void setVideoBar(visible).catch(() => undefined);
  }, [visible]);

  if (!offer) return null;
  const title = probe.data?.filename || offer.host;

  return (
    <div
      data-tauri-drag-region
      className="m-1 flex h-[calc(100vh-0.5rem)] items-center gap-2 rounded-xl border bg-card/95 px-3 text-sm shadow-lg select-none"
    >
      <Play data-tauri-drag-region className="size-5 shrink-0 text-primary" aria-hidden />
      <div data-tauri-drag-region className="min-w-0 flex-1">
        <p data-tauri-drag-region className="truncate font-medium" title={title}>
          {title}
        </p>
        <p data-tauri-drag-region className="truncate text-xs text-muted-foreground">
          {probe.isFetching ? t("bar.reading") : add.isSuccess ? t("drop.added") : offer.host}
        </p>
      </div>
      {probe.isFetching ? (
        <LoaderCircle className="size-4 shrink-0 animate-spin text-muted-foreground" aria-label={t("bar.reading")} />
      ) : (
        <>
          <Select
            aria-label={t("add.quality")}
            className="h-8 w-40 shrink-0"
            value={choice}
            onChange={(event) => setChoice(Number(event.target.value))}
          >
            {options.map((option, index) => (
              <option key={option.format || "best"} value={index}>
                {option.height === null
                  ? t("add.quality.best")
                  : t("add.quality.height", { height: option.height })}
                {option.size ? ` · ${bytes(i18n, option.size)}` : ""}
              </option>
            ))}
          </Select>
          <Button
            size="sm"
            disabled={add.isPending || add.isSuccess}
            onClick={() => add.mutate()}
            aria-label={t("bar.download")}
          >
            {add.isSuccess ? <Check /> : <Download />}
            {t("bar.download")}
          </Button>
        </>
      )}
      <Button size="icon" variant="ghost" className="size-8 shrink-0" aria-label={t("bar.dismiss")} title={t("bar.dismiss")} onClick={dismiss}>
        <X />
      </Button>
    </div>
  );
}
