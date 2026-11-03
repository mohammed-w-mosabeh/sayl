"use client";

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { Download } from "@/lib/api/client";
import { socketUrl, type Connection } from "@/lib/api/connection";
import type { Key } from "@/lib/i18n";
import { useUi } from "@/lib/prefs";

export interface DaemonEvent {
  event: string;
  id?: string;
  [field: string]: unknown;
}

export const DOWNLOADS = ["downloads"] as const;

/** Events that are worth a desktop notification, and the title each one gets. */
export const NOTIFY: Record<string, Key> = {
  "download.completed": "notify.completed",
  "download.failed": "notify.failed",
  "download.needs_refresh": "notify.needs_refresh",
  "download.quality_degraded": "notify.quality_degraded",
  "capture.detected": "notify.captured",
};

const num = (value: unknown, fallback: number) => (typeof value === "number" ? value : fallback);

/**
 * Apply an event to the cached list. Progress arrives several times a second, so it is patched
 * in place; anything else changes more than the event carries, and the list is fetched again.
 */
export function applyEvent(
  list: Download[] | undefined,
  event: DaemonEvent,
): { list: Download[] | undefined; refetch: boolean } {
  if (!list) return { list, refetch: true };
  if (event.event === "download.progress") {
    const index = list.findIndex((row) => row.id === event.id);
    if (index < 0) return { list, refetch: true };
    const next = [...list];
    const row = list[index] as Download;
    next[index] = {
      ...row,
      status: row.status === "queued" || row.status === "probing" ? "active" : row.status,
      done_bytes: num(event.done, row.done_bytes),
      total_bytes: num(event.total, row.total_bytes),
      speed: num(event.speed, row.speed),
      connections: num(event.connections, row.connections),
    };
    return { list: next, refetch: false };
  }
  if (event.event === "download.removed") {
    return { list: list.filter((row) => row.id !== event.id), refetch: false };
  }
  return { list, refetch: true };
}

export function parseEvent(raw: unknown): DaemonEvent | null {
  if (typeof raw !== "string") return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (value && typeof value === "object" && typeof (value as DaemonEvent).event === "string") {
      return value as DaemonEvent;
    }
  } catch {
    // Not an event.
  }
  return null;
}

export function backoff(attempt: number): number {
  return Math.min(10_000, 500 * 2 ** attempt);
}

function handle(client: QueryClient, event: DaemonEvent, refetch: () => void): void {
  const { list, refetch: needed } = applyEvent(client.getQueryData<Download[]>(DOWNLOADS), event);
  if (list) client.setQueryData(DOWNLOADS, list);
  if (event.id) void client.invalidateQueries({ queryKey: ["download", event.id] });
  if (event.event === "download.removed" && useUi.getState().selected === event.id) {
    useUi.getState().select(null);
  }
  if (needed) refetch();
}

/**
 * Follow the daemon's WebSocket for as long as the app is open, reconnecting with backoff.
 * `onEvent` sees every event after the cache is updated.
 */
export function useDaemonEvents(
  connection: Connection | null,
  onEvent?: (event: DaemonEvent) => void,
): void {
  const client = useQueryClient();
  const setLive = useUi((state) => state.setLive);

  useEffect(() => {
    if (!connection) return;
    let socket: WebSocket | null = null;
    let attempt = 0;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let pending: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;

    const refetch = () => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = undefined;
        void client.invalidateQueries({ queryKey: DOWNLOADS });
      }, 250);
    };

    const open = () => {
      socket = new WebSocket(socketUrl(connection), [`bearer.${connection.token}`]);
      socket.onopen = () => {
        attempt = 0;
        setLive(true);
        refetch(); // Anything that changed while disconnected.
      };
      socket.onmessage = (message) => {
        const event = parseEvent(message.data);
        if (!event) return;
        handle(client, event, refetch);
        onEvent?.(event);
      };
      socket.onclose = () => {
        setLive(false);
        if (stopped) return;
        retry = setTimeout(open, backoff(attempt));
        attempt += 1;
      };
    };

    open();
    return () => {
      stopped = true;
      clearTimeout(retry);
      clearTimeout(pending);
      socket?.close();
      setLive(false);
    };
  }, [client, connection, onEvent, setLive]);
}
