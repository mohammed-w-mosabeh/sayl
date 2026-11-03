"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowDownToLine, Check, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useApi } from "@/components/providers";
import { DOWNLOADS } from "@/lib/events";
import { isDownloadUrl } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { showMainWindow } from "@/lib/native";
import { cn } from "@/lib/utils";

/** Links dragged from a browser arrive as text/uri-list, or as plain text from other apps. */
export function droppedUrls(data: Pick<DataTransfer, "getData">): string[] {
  const list = data.getData("text/uri-list");
  const text = list || data.getData("text/plain");
  const urls = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  return [...new Set(urls)].filter(isDownloadUrl);
}

type State = "idle" | "over" | "added" | "rejected";

/**
 * The floating drop target. Its window has native drag-and-drop turned off, so the webview
 * receives the browser's drag data instead of Tauri's file-path events.
 */
export function DropTarget() {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const [state, setState] = useState<State>("idle");

  useEffect(() => {
    document.documentElement.classList.add("drop-window");
  }, []);

  useEffect(() => {
    if (state !== "added" && state !== "rejected") return;
    const timer = setTimeout(() => setState("idle"), 1500);
    return () => clearTimeout(timer);
  }, [state]);

  const add = useMutation({
    mutationFn: async (urls: string[]) => {
      for (const url of urls) await api.add({ url });
    },
    onSuccess: () => {
      setState("added");
      void client.invalidateQueries({ queryKey: DOWNLOADS });
    },
    onError: () => setState("rejected"),
  });

  const Icon = state === "added" ? Check : state === "rejected" ? X : ArrowDownToLine;
  const label = state === "added" ? t("drop.added") : state === "rejected" ? t("drop.rejected") : t("drop.hint");

  return (
    <div
      data-tauri-drag-region
      role="button"
      tabIndex={0}
      aria-label={label}
      title={label}
      onDoubleClick={() => void showMainWindow()}
      onKeyDown={(event) => {
        if (event.key === "Enter") void showMainWindow();
      }}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        setState("over");
      }}
      onDragLeave={() => setState("idle")}
      onDrop={(event) => {
        event.preventDefault();
        const urls = droppedUrls(event.dataTransfer);
        if (urls.length === 0) setState("rejected");
        else add.mutate(urls);
      }}
      className={cn(
        "m-1 flex h-[calc(100vh-0.5rem)] cursor-grab flex-col items-center justify-center gap-1 rounded-2xl border-2 bg-card/95 text-xs shadow-lg select-none",
        state === "over" && "scale-105 border-primary bg-accent",
        state === "added" && "border-success",
        state === "rejected" && "border-danger",
      )}
    >
      <Icon
        data-tauri-drag-region
        className={cn(
          "size-7",
          state === "added" ? "text-success" : state === "rejected" ? "text-danger" : "text-primary",
        )}
      />
      <span data-tauri-drag-region className="px-1 text-center leading-tight text-muted-foreground">
        {label}
      </span>
    </div>
  );
}
