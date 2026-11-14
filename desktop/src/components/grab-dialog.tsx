"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { extensionList, megabytes } from "@/lib/batch";
import { DOWNLOADS } from "@/lib/events";
import { bytes } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

function readable(url: string): string {
  try {
    return decodeURI(url);
  } catch {
    return url;
  }
}

function Results({ grabId, onDone }: { grabId: string; onDone: () => void }) {
  const i18n = useI18n();
  const { t } = i18n;
  const api = useApi();
  const client = useQueryClient();
  const grab = useQuery({
    queryKey: ["grab", grabId],
    queryFn: () => api.grabStatus(grabId),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1000 : false),
  });
  const files = grab.data?.files ?? [];
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const chosen = picked ?? new Set(files.map((file) => file.url));
  useEffect(() => () => void api.cancelGrab(grabId).catch(() => undefined), [api, grabId]);
  const queue = useMutation({
    mutationFn: () => api.batch({ urls: files.map((f) => f.url).filter((url) => chosen.has(url)) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: DOWNLOADS });
      onDone();
    },
  });
  const data = grab.data;
  if (!data || data.status === "running") {
    return (
      <p role="status" className="flex items-center gap-2 px-5 py-4 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        {t("grab.running", { pages: data?.pages ?? 0 })}
      </p>
    );
  }
  const toggle = (url: string) => {
    const next = new Set(chosen);
    if (next.has(url)) next.delete(url);
    else next.add(url);
    setPicked(next);
  };
  return (
    <div className="flex min-h-0 flex-col">
      <div className="grid gap-3 overflow-y-auto px-5 py-4">
        {data.status === "failed" ? (
          <p className="text-sm text-danger">{t("grab.failed", { error: data.error })}</p>
        ) : (
          <p role="status" className="text-sm text-muted-foreground">
            {t("grab.found", { files: files.length, pages: data.pages })}
            {data.skipped_robots ? ` · ${t("grab.robots", { count: data.skipped_robots })}` : ""}
          </p>
        )}
        {files.length ? (
          <>
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                checked={chosen.size === files.length}
                onChange={(event) => setPicked(new Set(event.target.checked ? files.map((f) => f.url) : []))}
              />
              {t("grab.select_all")}
            </label>
            <ul className="max-h-72 divide-y overflow-y-auto rounded-md border">
              {files.map((file) => (
                <li key={file.url}>
                  <label className="flex items-center gap-3 px-3 py-1.5 text-sm">
                    <input type="checkbox" checked={chosen.has(file.url)} onChange={() => toggle(file.url)} />
                    <bdi dir="ltr" className="min-w-0 flex-1 truncate font-mono text-xs" title={file.url}>
                      {readable(file.url)}
                    </bdi>
                    <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                      {file.size === null || file.size === undefined ? "" : bytes(i18n, file.size)}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </>
        ) : data.status === "done" ? (
          <p className="text-sm text-muted-foreground">{t("grab.none")}</p>
        ) : null}
        {queue.error ? <p className="text-sm text-danger">{queue.error.message}</p> : null}
      </div>
      <div className="flex justify-end gap-2 border-t px-5 py-3">
        <Button onClick={onDone}>{t("action.cancel")}</Button>
        <Button variant="primary" disabled={!chosen.size || queue.isPending} onClick={() => queue.mutate()}>
          {queue.isPending ? <Loader2 className="animate-spin" /> : null}
          {t("grab.download", { count: chosen.size })}
        </Button>
      </div>
    </div>
  );
}

function Form({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const [url, setUrl] = useState("");
  const [depth, setDepth] = useState("2");
  const [types, setTypes] = useState("");
  const [min, setMin] = useState("");
  const [max, setMax] = useState("");
  const start = useMutation({
    mutationFn: () =>
      api.grab({
        url: url.trim(),
        depth: Number(depth),
        extensions: extensionList(types),
        min_size: megabytes(min),
        max_size: megabytes(max),
      }),
  });
  if (start.data) return <Results grabId={start.data.id} onDone={onDone} />;
  const ready = /^https?:\/\/\S+$/i.test(url.trim()) && !start.isPending;
  return (
    <form
      className="flex min-h-0 flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) start.mutate();
      }}
    >
      <div className="grid gap-4 overflow-y-auto px-5 py-4">
        <Field label={t("grab.url")} htmlFor="grab-url">
          <Input id="grab-url" dir="ltr" autoFocus placeholder="https://" value={url} onChange={(e) => setUrl(e.target.value)} />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("grab.depth")} htmlFor="grab-depth">
            <Select id="grab-depth" value={depth} onChange={(e) => setDepth(e.target.value)}>
              {[0, 1, 2, 3, 4, 5].map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("grab.types")} htmlFor="grab-types" hint={t("grab.types.hint")}>
            <Input id="grab-types" dir="ltr" value={types} onChange={(e) => setTypes(e.target.value)} />
          </Field>
          <Field label={t("grab.min")} htmlFor="grab-min">
            <Input id="grab-min" dir="ltr" inputMode="decimal" value={min} onChange={(e) => setMin(e.target.value)} />
          </Field>
          <Field label={t("grab.max")} htmlFor="grab-max">
            <Input id="grab-max" dir="ltr" inputMode="decimal" value={max} onChange={(e) => setMax(e.target.value)} />
          </Field>
        </div>
        {start.error ? <p className="text-sm text-danger">{start.error.message}</p> : null}
      </div>
      <div className="flex justify-end gap-2 border-t px-5 py-3">
        <Button onClick={onDone}>{t("action.cancel")}</Button>
        <Button type="submit" variant="primary" disabled={!ready}>
          {start.isPending ? <Loader2 className="animate-spin" /> : null}
          {t("grab.start")}
        </Button>
      </div>
    </form>
  );
}

export function GrabDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useI18n();
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent title={t("grab.title")} description={t("grab.body")}>
        {open ? <Form onDone={onClose} /> : null}
      </DialogContent>
    </Dialog>
  );
}
