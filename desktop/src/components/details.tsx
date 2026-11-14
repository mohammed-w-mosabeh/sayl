"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Copy, FolderOpen, Link2Off, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { RowActions } from "@/components/actions";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import type { Download } from "@/lib/api/client";
import { DOWNLOADS } from "@/lib/events";
import { bytes, dateTime, duration, eta, isDownloadUrl, percent, speed } from "@/lib/format";
import { useI18n, type I18n } from "@/lib/i18n";
import { inTauri } from "@/lib/api/connection";
import { openFolder } from "@/lib/native";
import { useUi } from "@/lib/prefs";
import { statusKey, title, tone } from "@/lib/status";

export function modeLabel(i18n: I18n, row: Download): string {
  if (row.engine === "ytdlp") return i18n.t("details.mode.video");
  if (row.engine === "hybrid") {
    return row.connections > 1
      ? i18n.t("details.mode.accelerated", { count: row.connections })
      : i18n.t("details.mode.single");
  }
  return row.connections > 1
    ? i18n.t("details.mode.connections", { count: row.connections })
    : i18n.t("details.mode.single");
}

interface Media {
  height?: number | null;
  vcodec?: string;
  acodec?: string;
  has_audio?: boolean | null;
}

interface QualityWarning {
  items?: { title?: string; requested?: Media; delivered?: Media }[];
  solver_failures?: string[];
}

function media(i18n: I18n, value: Media | undefined): string {
  if (!value) return i18n.t("time.unknown");
  const parts = [
    value.height ? i18n.t("media.height", { height: value.height }) : "",
    value.vcodec ?? "",
    value.has_audio === false ? i18n.t("media.no_audio") : (value.acodec ?? ""),
  ].filter(Boolean);
  return parts.join(" · ") || i18n.t("time.unknown");
}

function Warning({ warning }: { warning: QualityWarning }) {
  const i18n = useI18n();
  return (
    <div className="grid gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
      <p className="flex items-center gap-2 font-medium">
        <AlertTriangle className="size-4 text-warning" />
        {i18n.t("details.warning.title")}
      </p>
      {warning.items?.map((item, index) => (
        <div key={index} className="grid gap-0.5">
          {item.title ? (
            <p dir="auto" className="truncate font-medium">
              {item.title}
            </p>
          ) : null}
          <p>
            <span className="text-muted-foreground">{i18n.t("details.warning.requested")}: </span>
            <bdi>{media(i18n, item.requested)}</bdi>
          </p>
          <p>
            <span className="text-muted-foreground">{i18n.t("details.warning.delivered")}: </span>
            <bdi>{media(i18n, item.delivered)}</bdi>
          </p>
        </div>
      ))}
      {warning.solver_failures?.length ? <p>{i18n.t("details.warning.solver")}</p> : null}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm break-words">{children}</dd>
    </div>
  );
}

function CopyLink({ url }: { url: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  return (
    <Button
      size="icon"
      variant="ghost"
      aria-label={t("action.copy_link")}
      title={copied ? t("action.copied") : t("action.copy_link")}
      onClick={() =>
        void navigator.clipboard.writeText(url).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        })
      }
    >
      {copied ? <Check /> : <Copy />}
    </Button>
  );
}

function Refresh({ row }: { row: Download }) {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const [url, setUrl] = useState("");
  const refresh = useMutation({
    mutationFn: () => api.refresh(row.id, url.trim()),
    onSuccess: () => {
      setUrl("");
      void client.invalidateQueries({ queryKey: DOWNLOADS });
    },
  });
  return (
    <form
      className="grid gap-2 rounded-md border border-warning/40 bg-warning/10 p-3"
      onSubmit={(event) => {
        event.preventDefault();
        refresh.mutate();
      }}
    >
      <p className="flex items-center gap-2 text-sm font-medium">
        <Link2Off className="size-4 text-warning" />
        {t("details.refresh.title")}
      </p>
      <p className="text-xs text-muted-foreground">{t("details.refresh.body")}</p>
      <Input
        dir="ltr"
        value={url}
        placeholder={t("add.url.placeholder")}
        onChange={(event) => setUrl(event.target.value)}
        aria-label={t("add.url")}
      />
      {refresh.error ? <p className="text-xs text-danger">{refresh.error.message}</p> : null}
      <Button type="submit" variant="primary" size="sm" disabled={!isDownloadUrl(url) || refresh.isPending}>
        {t("details.refresh.submit")}
      </Button>
    </form>
  );
}

function Limits({ row }: { row: Download }) {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const [limit, setLimit] = useState(String(Math.round(row.speed_limit / 1024)));
  const [priority, setPriority] = useState(String(row.priority));
  const save = useMutation({
    mutationFn: () =>
      api.patch(row.id, {
        speed_limit: Math.max(0, Math.round(Number(limit) || 0)) * 1024,
        priority: Math.round(Number(priority) || 0),
      }),
    onSuccess: () => void client.invalidateQueries({ queryKey: DOWNLOADS }),
  });
  return (
    <form
      className="grid grid-cols-2 items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <label className="grid gap-1 text-xs text-muted-foreground">
        {t("details.speed_limit")}
        <Input dir="ltr" inputMode="numeric" value={limit} onChange={(e) => setLimit(e.target.value)} />
      </label>
      <label className="grid gap-1 text-xs text-muted-foreground">
        {t("details.priority")}
        <Input dir="ltr" inputMode="numeric" value={priority} onChange={(e) => setPriority(e.target.value)} />
      </label>
      <Button type="submit" size="md" className="col-span-2" disabled={save.isPending}>
        {save.isSuccess ? <Check /> : t("action.save")}
      </Button>
    </form>
  );
}

/** Whether the file matches its checksum, and a way to check it against one. */
function Checksum({ row }: { row: Download }) {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const [value, setValue] = useState("");
  const verify = useMutation({
    mutationFn: () => api.verify(row.id, value.trim()),
    onSuccess: () => {
      setValue("");
      void client.invalidateQueries({ queryKey: DOWNLOADS });
    },
  });
  const algo = row.checksum_algo || "sha256";
  const state =
    row.checksum_ok === true
      ? { text: t("details.checksum.ok", { algo }), className: "text-success" }
      : row.checksum_ok === false
        ? { text: t("details.checksum.bad", { algo }), className: "text-danger" }
        : { text: t("details.checksum.unchecked"), className: "text-muted-foreground" };
  return (
    <div className="grid gap-1.5">
      <p className={`text-sm ${state.className}`} role="status">
        {state.text}
      </p>
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          verify.mutate();
        }}
      >
        <Input
          dir="ltr"
          aria-label={t("details.checksum")}
          placeholder={row.checksum_expected ? row.checksum_expected.slice(0, 16) + "…" : "sha256:…"}
          className="h-8 font-mono text-xs"
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <Button type="submit" size="sm" disabled={verify.isPending || (!value.trim() && !row.checksum_expected)}>
          {t("details.verify")}
        </Button>
      </form>
      {verify.error ? <p className="text-xs text-danger">{verify.error.message}</p> : null}
    </div>
  );
}

export function Details({ row }: { row: Download }) {
  const i18n = useI18n();
  const { t } = i18n;
  const select = useUi((state) => state.select);
  const value =
    row.status === "completed"
      ? 100
      : (percent(row.done_bytes, row.total_bytes) ?? (row.status === "active" ? null : 0));
  const warning = row.warnings?.quality_degraded as QualityWarning | undefined;
  const finished = ["completed", "failed", "canceled"].includes(row.status);

  return (
    <aside className="flex w-80 shrink-0 flex-col overflow-y-auto border-s bg-card" aria-label={t("details.title")}>
      <div className="flex items-start justify-between gap-2 border-b p-4">
        <h2 dir="auto" className="min-w-0 text-sm font-semibold break-words">
          {title(row)}
        </h2>
        <Button size="icon" variant="ghost" aria-label={t("action.close")} onClick={() => select(null)}>
          <X />
        </Button>
      </div>
      <div className="grid gap-4 p-4">
        <div className="grid gap-1.5">
          <div className="flex items-center justify-between text-sm">
            <span>{t(statusKey(row))}</span>
            <span className="text-muted-foreground tabular-nums">
              {row.status === "active" ? speed(i18n, row.speed) : ""}
            </span>
          </div>
          <Progress value={value} tone={tone(row)} />
          <p className="text-xs text-muted-foreground tabular-nums">
            {t("details.progress", {
              done: bytes(i18n, row.done_bytes),
              total: row.total_bytes > 0 ? bytes(i18n, row.total_bytes) : t("time.unknown"),
            })}
            {row.status === "active"
              ? ` · ${duration(i18n, eta(row.done_bytes, row.total_bytes, row.speed))}`
              : ""}
          </p>
        </div>
        <RowActions row={row} />
        {row.status === "needs_refresh" ? <Refresh row={row} /> : null}
        {warning ? <Warning warning={warning} /> : null}
        <dl className="grid gap-3">
          <Row label={t("details.mode")}>{modeLabel(i18n, row)}</Row>
          <Row label={t("add.engine")}>{t(`engine.${row.engine}`)}</Row>
          <Row label={t("details.url")}>
            <div className="flex items-start gap-1">
              <bdi dir="ltr" className="min-w-0 flex-1 font-mono text-xs break-all select-text">
                {row.url}
              </bdi>
              <CopyLink url={row.url} />
            </div>
          </Row>
          {row.page_url ? (
            <Row label={t("details.page")}>
              <bdi dir="ltr" className="font-mono text-xs break-all select-text">
                {row.page_url}
              </bdi>
            </Row>
          ) : null}
          <Row label={t("details.dest")}>
            <div className="flex items-start gap-1">
              <bdi dir="ltr" className="min-w-0 flex-1 font-mono text-xs break-all">
                {row.dest_dir}
              </bdi>
              {inTauri() ? (
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={t("action.open_folder")}
                  title={t("action.open_folder")}
                  onClick={() => void openFolder(row.dest_dir)}
                >
                  <FolderOpen />
                </Button>
              ) : null}
            </div>
          </Row>
          <Row label={t("details.added")}>{dateTime(i18n, row.created_at)}</Row>
          {row.started_at ? <Row label={t("details.started")}>{dateTime(i18n, row.started_at)}</Row> : null}
          {row.finished_at ? <Row label={t("details.finished")}>{dateTime(i18n, row.finished_at)}</Row> : null}
          {row.status === "completed" ? (
            <Row label={t("details.checksum")}>
              <Checksum key={row.id} row={row} />
            </Row>
          ) : null}
          {row.error ? (
            <Row label={t("details.error")}>
              <span dir="auto" className="text-danger">
                {row.error}
              </span>
            </Row>
          ) : null}
        </dl>
        {finished ? null : <Limits key={row.id} row={row} />}
      </div>
    </aside>
  );
}
