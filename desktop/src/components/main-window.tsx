"use client";

import { useQuery } from "@tanstack/react-query";
import { BarChart3, Globe, ListPlus, Plus, Search, Settings } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { AddDialog } from "@/components/add-dialog";
import { BatchDialog } from "@/components/batch-dialog";
import { Details } from "@/components/details";
import { DownloadList } from "@/components/download-list";
import { GrabDialog } from "@/components/grab-dialog";
import { useApi } from "@/components/providers";
import { QueueBanner, QueueToggle } from "@/components/queue-controls";
import { Sidebar } from "@/components/sidebar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DOWNLOADS } from "@/lib/events";
import { isDownloadUrl } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { useUi } from "@/lib/prefs";
import { matches, search, sorted } from "@/lib/status";
import { cn } from "@/lib/utils";

function editing(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

/** Ctrl+N opens Add; pasting a link anywhere outside a field opens Add with it. */
function useShortcuts() {
  const openAdd = useUi((state) => state.openAdd);
  const adding = useUi((state) => state.adding.open);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        openAdd();
      }
    };
    const onPaste = (event: ClipboardEvent) => {
      if (adding || editing(event.target)) return;
      const text = event.clipboardData?.getData("text/plain")?.trim() ?? "";
      if (isDownloadUrl(text)) openAdd(text);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("paste", onPaste);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("paste", onPaste);
    };
  }, [adding, openAdd]);
}

export function MainWindow() {
  const { t } = useI18n();
  const api = useApi();
  const { filter, category, search: text, setSearch, selected, openAdd, live } = useUi();
  const downloads = useQuery({ queryKey: DOWNLOADS, queryFn: api.downloads, refetchInterval: 15_000 });
  useShortcuts();

  const all = useMemo(() => sorted(downloads.data ?? []), [downloads.data]);
  const visible = useMemo(
    () =>
      search(
        all.filter((row) => (category ? row.category_id === category : matches(row, filter))),
        text,
      ),
    [all, category, filter, text],
  );
  const current = all.find((row) => row.id === selected);
  const [dialog, setDialog] = useState<"batch" | "grab" | null>(null);

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b bg-card px-4 py-2.5">
        <div className="flex items-center gap-2 pe-2">
          <span className="flex size-7 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
            S
          </span>
          <span className="font-semibold">{t("app.name")}</span>
        </div>
        <Button variant="primary" onClick={() => openAdd()}>
          <Plus />
          {t("action.add")}
        </Button>
        <Button size="icon" variant="ghost" aria-label={t("action.batch")} title={t("action.batch")} onClick={() => setDialog("batch")}>
          <ListPlus />
        </Button>
        <Button size="icon" variant="ghost" aria-label={t("action.grab")} title={t("action.grab")} onClick={() => setDialog("grab")}>
          <Globe />
        </Button>
        <QueueToggle />
        <div className="relative ms-auto w-64">
          <Search className="pointer-events-none absolute start-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={text}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("list.search")}
            aria-label={t("list.search")}
            className="ps-8"
          />
        </div>
        <span
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          role="status"
          title={live ? t("daemon.live") : t("daemon.reconnecting")}
        >
          <span className={cn("size-2 rounded-full", live ? "bg-success" : "animate-pulse bg-warning")} />
          <span className="sr-only">{live ? t("daemon.live") : t("daemon.reconnecting")}</span>
        </span>
        <Button asChild size="icon" variant="ghost" aria-label={t("nav.stats")} title={t("nav.stats")}>
          <Link href="/stats/">
            <BarChart3 />
          </Link>
        </Button>
        <Button asChild size="icon" variant="ghost" aria-label={t("nav.settings")} title={t("nav.settings")}>
          <Link href="/settings/">
            <Settings />
          </Link>
        </Button>
      </header>
      <div className="flex min-h-0 flex-1">
        <Sidebar rows={all} />
        <main className="flex min-w-0 flex-1 flex-col">
          <QueueBanner />
          <DownloadList rows={visible} empty={all.length === 0 ? "none" : "filtered"} />
        </main>
        {current ? <Details key={current.id} row={current} /> : null}
      </div>
      <AddDialog />
      <BatchDialog open={dialog === "batch"} onClose={() => setDialog(null)} />
      <GrabDialog open={dialog === "grab"} onClose={() => setDialog(null)} />
    </div>
  );
}
