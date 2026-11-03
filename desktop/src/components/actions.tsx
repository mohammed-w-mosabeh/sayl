"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pause, Play, Trash2 } from "lucide-react";
import { useState } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent } from "@/components/ui/dialog";
import type { Download } from "@/lib/api/client";
import { DOWNLOADS } from "@/lib/events";
import { useI18n } from "@/lib/i18n";
import { canPause, canResume, title } from "@/lib/status";

export function useDownloadActions() {
  const api = useApi();
  const client = useQueryClient();
  const settle = (row?: Download) => {
    if (row) {
      client.setQueryData<Download[]>(DOWNLOADS, (list) =>
        list?.map((item) => (item.id === row.id ? row : item)),
      );
    }
    void client.invalidateQueries({ queryKey: DOWNLOADS });
  };
  return {
    pause: useMutation({ mutationFn: api.pause, onSuccess: settle }),
    resume: useMutation({ mutationFn: api.resume, onSuccess: settle }),
    remove: useMutation({
      mutationFn: api.remove,
      onSuccess: (_, id) => {
        client.setQueryData<Download[]>(DOWNLOADS, (list) => list?.filter((row) => row.id !== id));
        settle();
      },
    }),
  };
}

export function RowActions({ row }: { row: Download }) {
  const { t } = useI18n();
  const { pause, resume, remove } = useDownloadActions();
  const [confirming, setConfirming] = useState(false);
  const stop = (event: React.MouseEvent) => event.stopPropagation();

  return (
    <div className="flex items-center justify-end gap-0.5" onClick={stop}>
      {canPause(row) ? (
        <Button
          size="icon"
          variant="ghost"
          aria-label={t("action.pause")}
          title={t("action.pause")}
          onClick={() => pause.mutate(row.id)}
        >
          <Pause />
        </Button>
      ) : null}
      {canResume(row) ? (
        <Button
          size="icon"
          variant="ghost"
          aria-label={t("action.resume")}
          title={t("action.resume")}
          onClick={() => resume.mutate(row.id)}
        >
          <Play />
        </Button>
      ) : null}
      <Button
        size="icon"
        variant="ghost"
        aria-label={t("action.remove")}
        title={t("action.remove")}
        onClick={() => setConfirming(true)}
      >
        <Trash2 />
      </Button>
      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent title={t("action.remove")} description={t("action.remove.confirm")}>
          <p dir="auto" className="truncate px-5 pt-4 text-sm font-medium">
            {title(row)}
          </p>
          <div className="flex justify-end gap-2 px-5 py-4">
            <DialogClose asChild>
              <Button>{t("action.cancel")}</Button>
            </DialogClose>
            <Button
              variant="danger"
              onClick={() => {
                remove.mutate(row.id);
                setConfirming(false);
              }}
            >
              {t("action.remove")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
