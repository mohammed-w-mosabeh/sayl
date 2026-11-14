"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Loader2, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { Checkbox, Switch } from "@/components/ui/switch";
import type { AddDownload, Probe } from "@/lib/api/client";
import { inTauri } from "@/lib/api/connection";
import { DOWNLOADS } from "@/lib/events";
import { bytes, isDownloadUrl } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { pickFolder } from "@/lib/native";
import { useUi } from "@/lib/prefs";
import { isProtected, playlistItems, qualities, type Format } from "@/lib/quality";

export interface Choices {
  filename: string;
  quality: number;
  audioOnly: boolean;
  subtitles: string[];
  items: Set<number>;
  destDir: string;
  limit: string;
  paused: boolean;
  checksum: string;
  browser: string;
}

/** The request the daemon gets. Exported for tests: this is where the picker meets the API. */
export function buildRequest(url: string, probe: Probe | undefined, choices: Choices): AddDownload {
  const formats = (probe?.formats ?? []) as Format[];
  const options = qualities(formats);
  const entries = probe?.entries ?? [];
  const body: AddDownload = {
    url: url.trim(),
    filename: probe?.engine === "ytdlp" ? "" : choices.filename.trim(),
    dest_dir: choices.destDir.trim(),
    start_paused: choices.paused,
    speed_limit: Math.max(0, Math.round(Number(choices.limit) || 0)) * 1024,
  };
  if (choices.checksum.trim()) body.checksum = choices.checksum.trim();
  if (choices.browser) body.cookies_from_browser = choices.browser;
  if (probe?.engine === "ytdlp") {
    body.audio_only = choices.audioOnly;
    if (!choices.audioOnly) body.format_id = options[choices.quality]?.format ?? "";
    if (choices.subtitles.length) body.subtitles = choices.subtitles;
    if (entries.length && choices.items.size < entries.length) {
      body.playlist_items = playlistItems(choices.items);
    }
  }
  return body;
}

function useDebounced<T>(value: T, delay: number): T {
  const [current, setCurrent] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setCurrent(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return current;
}

function Form({ initialUrl, onDone }: { initialUrl: string; onDone: () => void }) {
  const i18n = useI18n();
  const { t } = i18n;
  const api = useApi();
  const client = useQueryClient();
  const select = useUi((state) => state.select);
  const info = useQuery({ queryKey: ["info"], queryFn: api.info });

  const [url, setUrl] = useState(initialUrl);
  const target = useDebounced(url.trim(), 500);
  const valid = isDownloadUrl(target);
  const probe = useQuery({
    queryKey: ["probe", target],
    queryFn: () => api.probe(target),
    enabled: valid && !target.startsWith("magnet:"),
    retry: false,
    staleTime: 60_000,
  });

  // What the person changed. Anything left unset follows the probe and the daemon's defaults,
  // and media choices start over whenever a different link is probed.
  const [overrides, setOverrides] = useState<Partial<Choices>>({});
  const [probed, setProbed] = useState(probe.data);
  if (probe.data !== probed) {
    setProbed(probe.data);
    setOverrides(({ destDir, limit, paused, checksum, browser }) => ({ destDir, limit, paused, checksum, browser }));
  }
  const choices: Choices = {
    filename: probe.data?.filename ?? "",
    quality: 0,
    audioOnly: false,
    subtitles: [],
    items: new Set((probe.data?.entries ?? []).map((_, index) => index + 1)),
    destDir: info.data?.default_dir ?? "",
    limit: "0",
    paused: false,
    checksum: "",
    browser: "",
    ...Object.fromEntries(Object.entries(overrides).filter(([, value]) => value !== undefined)),
  };
  const change = (values: Partial<Choices>) => setOverrides((current) => ({ ...current, ...values }));

  const formats = useMemo(() => (probe.data?.formats ?? []) as Format[], [probe.data]);
  const options = useMemo(() => qualities(formats), [formats]);
  const entries = probe.data?.entries ?? [];
  const video = probe.data?.engine === "ytdlp";
  const locked = isProtected(formats);

  const browsers = useQuery({ queryKey: ["browsers"], queryFn: api.browsers, staleTime: 300_000 });
  const add = useMutation({
    mutationFn: () => {
      const body = buildRequest(url, probe.data, choices);
      // The folder shown is the default; send one only when chosen, so a category's folder wins.
      if (overrides.destDir === undefined) body.dest_dir = "";
      return api.add(body);
    },
    onSuccess: (row) => {
      void client.invalidateQueries({ queryKey: DOWNLOADS });
      select(row.id);
      onDone();
    },
  });

  const urlOk = isDownloadUrl(url);
  const ready = urlOk && !locked && !add.isPending && (target.startsWith("magnet:") || !probe.isFetching);

  return (
    <form
      className="flex min-h-0 flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) add.mutate();
      }}
    >
      <div className="grid gap-4 overflow-y-auto px-5 py-4">
        <Field label={t("add.url")} htmlFor="add-url" hint={url && !urlOk ? t("add.invalid_url") : undefined}>
          <Input
            id="add-url"
            dir="ltr"
            autoFocus
            value={url}
            placeholder={t("add.url.placeholder")}
            onChange={(event) => setUrl(event.target.value)}
          />
        </Field>

        {probe.isFetching ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("add.checking")}
          </p>
        ) : null}
        {probe.error ? (
          <p className="text-sm text-danger">{t("add.probe_failed", { error: probe.error.message })}</p>
        ) : null}

        {probe.data && !probe.isFetching ? (
          <div className="grid gap-4">
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
              <span>
                <span className="text-muted-foreground">{t("add.size")}: </span>
                {probe.data.total ? bytes(i18n, probe.data.total) : t("add.size.unknown")}
              </span>
              <span>
                <span className="text-muted-foreground">{t("add.engine")}: </span>
                {t(`engine.${probe.data.engine}`)}
              </span>
              <span className="text-muted-foreground">
                {probe.data.resumable ? t("add.resumable") : t("add.not_resumable")}
              </span>
            </div>

            {locked ? (
              <p className="flex items-center gap-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm">
                <ShieldAlert className="size-4 text-danger" />
                {t("add.protected")}
              </p>
            ) : null}

            {video ? (
              <p dir="auto" className="text-sm font-medium">
                {probe.data.filename}
              </p>
            ) : (
              <Field label={t("add.filename")} htmlFor="add-name">
                <Input
                  id="add-name"
                  dir="auto"
                  value={choices.filename}
                  onChange={(event) => change({ filename: event.target.value })}
                />
              </Field>
            )}

            {video && !locked ? (
              <>
                <div className="grid grid-cols-[1fr_auto] items-end gap-4">
                  <Field label={t("add.quality")} htmlFor="add-quality">
                    <Select
                      id="add-quality"
                      value={choices.quality}
                      disabled={choices.audioOnly || options.length < 2}
                      onChange={(event) => change({ quality: Number(event.target.value) })}
                    >
                      {options.map((option, index) => (
                        <option key={option.format || "best"} value={index}>
                          {option.height === null
                            ? t("add.quality.best")
                            : t("add.quality.height", { height: option.height })}
                          {option.fps && option.fps > 30 ? ` ${option.fps}fps` : ""}
                          {option.size ? ` · ${bytes(i18n, option.size)}` : ""}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <label className="flex h-9 items-center gap-2 text-sm">
                    <Switch
                      checked={choices.audioOnly}
                      onCheckedChange={(checked) => change({ audioOnly: checked })}
                    />
                    {t("add.audio_only")}
                  </label>
                </div>

                {probe.data.subtitles.length ? (
                  <fieldset className="grid gap-2">
                    <legend className="mb-1.5 text-sm font-medium">{t("add.subtitles")}</legend>
                    <div className="flex flex-wrap gap-x-4 gap-y-2">
                      {probe.data.subtitles.map((language) => (
                        <label key={language} className="flex items-center gap-1.5 text-sm">
                          <Checkbox
                            checked={choices.subtitles.includes(language)}
                            onCheckedChange={(checked) =>
                              change({
                                subtitles: checked
                                  ? [...choices.subtitles, language]
                                  : choices.subtitles.filter((item) => item !== language),
                              })
                            }
                          />
                          <bdi>{language}</bdi>
                        </label>
                      ))}
                    </div>
                  </fieldset>
                ) : null}

                {entries.length ? (
                  <fieldset className="grid gap-2">
                    <div className="flex items-center justify-between">
                      <legend className="text-sm font-medium">{t("add.playlist")}</legend>
                      <div className="flex items-center gap-1 text-xs text-muted-foreground">
                        {t("add.playlist.count", { selected: choices.items.size, total: entries.length })}
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => change({ items: new Set(entries.map((_, i) => i + 1)) })}
                        >
                          {t("add.playlist.all")}
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => change({ items: new Set() })}>
                          {t("add.playlist.none")}
                        </Button>
                      </div>
                    </div>
                    <ul className="max-h-48 overflow-y-auto rounded-md border">
                      {entries.map((entry, i) => {
                        const index = typeof entry.index === "number" ? entry.index : i + 1;
                        return (
                          <li key={index} className="border-b last:border-b-0">
                            <label className="flex items-center gap-2 px-3 py-1.5 text-sm">
                              <Checkbox
                                checked={choices.items.has(index)}
                                onCheckedChange={(checked) => {
                                  const items = new Set(choices.items);
                                  if (checked) items.add(index);
                                  else items.delete(index);
                                  change({ items });
                                }}
                              />
                              <span className="w-6 text-xs text-muted-foreground tabular-nums">{index}</span>
                              <span dir="auto" className="min-w-0 flex-1 truncate">
                                {String(entry.title ?? entry.id ?? "")}
                              </span>
                            </label>
                          </li>
                        );
                      })}
                    </ul>
                  </fieldset>
                ) : null}
              </>
            ) : null}
          </div>
        ) : null}

        <Field label={t("add.dest")} htmlFor="add-dest">
          <div className="flex gap-2">
            <Input
              id="add-dest"
              dir="ltr"
              value={choices.destDir}
              onChange={(event) => change({ destDir: event.target.value })}
            />
            {inTauri() ? (
              <Button
                aria-label={t("action.browse")}
                title={t("action.browse")}
                onClick={() =>
                  void pickFolder(choices.destDir).then((folder) => folder && change({ destDir: folder }))
                }
              >
                <FolderOpen />
              </Button>
            ) : null}
          </div>
        </Field>

        <div className="grid grid-cols-[1fr_auto] items-end gap-4">
          <Field label={t("add.speed_limit")} htmlFor="add-limit">
            <Input
              id="add-limit"
              dir="ltr"
              inputMode="numeric"
              value={choices.limit}
              onChange={(event) => change({ limit: event.target.value })}
            />
          </Field>
          <label className="flex h-9 items-center gap-2 text-sm">
            <Switch checked={choices.paused} onCheckedChange={(checked) => change({ paused: checked })} />
            {t("add.start_paused")}
          </label>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("add.checksum")} htmlFor="add-checksum" hint={t("add.checksum.hint")}>
            <Input
              id="add-checksum"
              dir="ltr"
              className="font-mono text-xs"
              spellCheck={false}
              value={choices.checksum}
              onChange={(event) => change({ checksum: event.target.value })}
            />
          </Field>
          {browsers.data?.length ? (
            <Field label={t("add.browser_cookies")} htmlFor="add-browser">
              <Select
                id="add-browser"
                value={choices.browser}
                onChange={(event) => change({ browser: event.target.value })}
              >
                <option value="">{t("add.browser_cookies.none")}</option>
                {browsers.data.map((name) => (
                  <option key={name} value={name}>
                    {name[0]?.toUpperCase() + name.slice(1)}
                  </option>
                ))}
              </Select>
            </Field>
          ) : null}
        </div>
        {add.error ? <p className="text-sm text-danger">{add.error.message}</p> : null}
      </div>
      <div className="flex justify-end gap-2 border-t px-5 py-3">
        <Button onClick={onDone}>{t("action.cancel")}</Button>
        <Button type="submit" variant="primary" disabled={!ready}>
          {add.isPending ? <Loader2 className="animate-spin" /> : null}
          {t("add.submit")}
        </Button>
      </div>
    </form>
  );
}

export function AddDialog() {
  const { t } = useI18n();
  const { adding, closeAdd } = useUi();
  return (
    <Dialog open={adding.open} onOpenChange={(open) => (open ? undefined : closeAdd())}>
      <DialogContent title={t("add.title")}>
        {adding.open ? <Form initialUrl={adding.url} onDone={closeAdd} /> : null}
      </DialogContent>
    </Dialog>
  );
}
