"use client";

import { AlertTriangle, FileDown, Film } from "lucide-react";
import { RowActions } from "@/components/actions";
import { Progress } from "@/components/ui/progress";
import type { Download } from "@/lib/api/client";
import { bytes, duration, eta, percent, speed } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { useUi } from "@/lib/prefs";
import { degraded, statusKey, title, tone } from "@/lib/status";
import { cn } from "@/lib/utils";

function Row({ row, selected }: { row: Download; selected: boolean }) {
  const i18n = useI18n();
  const select = useUi((state) => state.select);
  const running = row.status === "active";
  // An unknown size pulses only while bytes are moving; a stopped download shows an empty bar.
  const value =
    row.status === "completed" ? 100 : (percent(row.done_bytes, row.total_bytes) ?? (running ? null : 0));
  const left = running ? eta(row.done_bytes, row.total_bytes, row.speed) : null;
  const Icon = row.engine === "ytdlp" ? Film : FileDown;

  return (
    <tr
      onClick={() => select(row.id)}
      aria-selected={selected}
      className={cn(
        "group cursor-default border-b last:border-b-0",
        selected ? "bg-accent" : "hover:bg-muted/60",
      )}
    >
      <td className="max-w-0 py-2.5 ps-4 pe-2">
        <div className="flex items-center gap-3">
          <Icon className="size-5 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            {/* File names keep their own direction inside an Arabic layout. */}
            <p dir="auto" className="truncate text-sm font-medium" title={title(row)}>
              {title(row)}
            </p>
            <p className="flex items-center gap-1 text-xs text-muted-foreground">
              {degraded(row) ? <AlertTriangle className="size-3 text-warning" /> : null}
              <span className={cn(tone(row) === "danger" && "text-danger")}>
                {i18n.t(statusKey(row))}
              </span>
              {row.error && row.status !== "completed" ? (
                <span dir="auto" className="truncate">
                  · {row.error}
                </span>
              ) : null}
            </p>
          </div>
        </div>
      </td>
      <td className="px-2 text-sm whitespace-nowrap text-muted-foreground tabular-nums">
        {row.total_bytes > 0 ? bytes(i18n, row.total_bytes) : i18n.t("time.unknown")}
      </td>
      <td className="px-2">
        <Progress value={value} tone={tone(row)} />
        <p className="mt-1 truncate text-xs text-muted-foreground tabular-nums">
          {row.total_bytes > 0 ? `${Math.floor(value ?? 0)}%` : bytes(i18n, row.done_bytes)}
          {left === null ? "" : ` · ${duration(i18n, left)}`}
        </p>
      </td>
      <td className="px-2 text-sm whitespace-nowrap tabular-nums">
        {running ? speed(i18n, row.speed) : ""}
      </td>
      <td className="pe-3">
        <div className="opacity-60 group-hover:opacity-100 group-aria-selected:opacity-100">
          <RowActions row={row} />
        </div>
      </td>
    </tr>
  );
}

export function DownloadList({ rows, empty }: { rows: Download[]; empty: "none" | "filtered" }) {
  const { t } = useI18n();
  const selected = useUi((state) => state.selected);

  if (rows.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
        <FileDown className="size-10 text-muted-foreground/60" />
        {empty === "none" ? (
          <>
            <p className="font-medium">{t("list.empty.title")}</p>
            <p className="max-w-sm text-sm text-muted-foreground">{t("list.empty.body")}</p>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">{t("list.empty.filtered")}</p>
        )}
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <table className="w-full table-fixed border-collapse">
        <colgroup>
          <col />
          <col className="w-24" />
          <col className="w-36" />
          <col className="w-24" />
          <col className="w-28" />
        </colgroup>
        <thead className="sticky top-0 z-10 bg-background/95 backdrop-blur">
          <tr className="border-b text-start text-xs text-muted-foreground">
            <th className="py-2 ps-4 pe-2 text-start font-medium">{t("list.column.name")}</th>
            <th className="px-2 text-start font-medium">{t("list.column.size")}</th>
            <th className="px-2 text-start font-medium">{t("list.column.progress")}</th>
            <th className="px-2 text-start font-medium">{t("list.column.speed")}</th>
            <th className="pe-3" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Row key={row.id} row={row} selected={row.id === selected} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
