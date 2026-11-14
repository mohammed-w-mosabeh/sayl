"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CirclePause, CirclePlay, Power } from "lucide-react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import type { QueueState } from "@/lib/api/client";
import { QUEUE } from "@/lib/events";
import { useI18n, type Key } from "@/lib/i18n";

function useQueue() {
  const api = useApi();
  const client = useQueryClient();
  const state = useQuery({ queryKey: QUEUE, queryFn: api.queue, refetchInterval: 10_000 });
  const settle = (data: QueueState) => client.setQueryData(QUEUE, data);
  return { state: state.data, settle };
}

/** Start or stop the queue: stopping pauses what runs and starts nothing new. */
export function QueueToggle() {
  const { t } = useI18n();
  const api = useApi();
  const { state, settle } = useQueue();
  const toggle = useMutation({
    mutationFn: () => (state?.running === false ? api.startQueue() : api.stopQueue()),
    onSuccess: settle,
  });
  const stopped = state?.running === false;
  const label = stopped ? t("queue.start") : t("queue.stop");
  return (
    <Button size="icon" variant="ghost" aria-label={label} title={label} disabled={!state || toggle.isPending} onClick={() => toggle.mutate()}>
      {stopped ? <CirclePlay /> : <CirclePause />}
    </Button>
  );
}

/** A stopped queue, or a post action waiting to run, shown above the list with a way out. */
export function QueueBanner() {
  const { t } = useI18n();
  const api = useApi();
  const { state, settle } = useQueue();
  const cancel = useMutation({ mutationFn: api.cancelPostAction, onSuccess: settle });
  const start = useMutation({ mutationFn: api.startQueue, onSuccess: settle });
  if (!state) return null;
  const action = t(`post.${state.post_action}` as Key);
  if (state.post_action_pending || state.post_action_armed) {
    return (
      <div role="alert" className="flex items-center gap-3 border-b bg-warning/15 px-4 py-2 text-sm">
        <Power className="size-4 shrink-0 text-warning" />
        <span className="flex-1">
          {t(state.post_action_pending ? "queue.post.pending" : "queue.post.armed", { action })}
        </span>
        <Button size="sm" variant={state.post_action_pending ? "danger" : "secondary"} onClick={() => cancel.mutate()} disabled={cancel.isPending}>
          {t("queue.post.cancel")}
        </Button>
      </div>
    );
  }
  if (!state.running) {
    return (
      <div role="status" className="flex items-center gap-3 border-b bg-muted px-4 py-2 text-sm">
        <CirclePause className="size-4 shrink-0 text-muted-foreground" />
        <span className="flex-1">{t("queue.stopped")}</span>
        <Button size="sm" onClick={() => start.mutate()} disabled={start.isPending}>
          {t("queue.start")}
        </Button>
      </div>
    );
  }
  return null;
}
