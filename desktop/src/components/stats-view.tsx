"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import type { Stats } from "@/lib/api/client";
import { categoryName } from "@/lib/categories";
import { bytes, speed } from "@/lib/format";
import { useI18n, type Key } from "@/lib/i18n";

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="grid content-start gap-3 rounded-lg border bg-card p-5">
      <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
      {children}
    </section>
  );
}

/** Horizontal bars: label, a bar scaled to the largest value, and the size. */
function Bars({ rows }: { rows: { label: ReactNode; key: string; value: number }[] }) {
  const i18n = useI18n();
  const largest = Math.max(1, ...rows.map((row) => row.value));
  return (
    <ul className="grid gap-2">
      {rows.map((row) => (
        <li key={row.key} className="grid grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-3 text-sm">
          <span className="truncate">{row.label}</span>
          <span className="h-2 rounded-full bg-muted">
            <span
              className="block h-2 rounded-full bg-primary"
              style={{ width: `${(row.value / largest) * 100}%` }}
            />
          </span>
          <span className="tabular-nums text-muted-foreground">{bytes(i18n, row.value)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Thirty columns, oldest first; the direction follows the page, so Arabic reads right to left. */
function Month({ days }: { days: Stats["per_day"] }) {
  const i18n = useI18n();
  const largest = Math.max(1, ...days.map((day) => day.bytes));
  return (
    <div className="flex h-32 items-end gap-1" role="img" aria-label={i18n.t("stats.month")}>
      {days.map((day) => (
        <span
          key={day.day}
          className="flex-1 rounded-t bg-primary/80"
          style={{ height: `${Math.max(2, (day.bytes / largest) * 100)}%` }}
          title={`${day.day}: ${bytes(i18n, day.bytes)}`}
        />
      ))}
    </div>
  );
}

export function StatsView() {
  const i18n = useI18n();
  const { t } = i18n;
  const api = useApi();
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 10_000 });
  const data = stats.data;
  const month = data ? data.per_day.reduce((sum, day) => sum + day.bytes, 0) : 0;
  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b bg-card px-4 py-2.5">
        <Button asChild size="icon" variant="ghost" aria-label={t("nav.back")} title={t("nav.back")}>
          <Link href="/">
            <ArrowLeft className="rtl:-scale-x-100" />
          </Link>
        </Button>
        <h1 className="font-semibold">{t("stats.title")}</h1>
      </header>
      <main className="flex-1 overflow-y-auto">
        {data ? (
          <div className="mx-auto grid max-w-3xl gap-5 p-6">
            <div className="grid gap-5 sm:grid-cols-3">
              <Card title={t("stats.completed")}>
                <p className="text-2xl font-semibold tabular-nums">{data.completed}</p>
              </Card>
              <Card title={t("stats.downloaded")}>
                <p className="text-2xl font-semibold tabular-nums">{bytes(i18n, data.completed_bytes)}</p>
              </Card>
              <Card title={t("stats.average")}>
                <p className="text-2xl font-semibold tabular-nums">
                  {speed(i18n, data.average_speed) || "—"}
                </p>
              </Card>
            </div>
            <Card title={`${t("stats.month")} · ${bytes(i18n, month)}`}>
              <Month days={data.per_day} />
            </Card>
            {data.top_hosts.length ? (
              <Card title={t("stats.hosts")}>
                <Bars
                  rows={data.top_hosts.map((row) => ({
                    key: row.host,
                    label: (
                      <bdi dir="ltr" className="font-mono">
                        {row.host}
                      </bdi>
                    ),
                    value: row.bytes,
                  }))}
                />
              </Card>
            ) : (
              <p className="text-sm text-muted-foreground">{t("stats.empty")}</p>
            )}
            {data.by_category.length ? (
              <Card title={t("stats.categories")}>
                <Bars
                  rows={data.by_category.map((row) => ({
                    key: row.category || "-",
                    label: row.category ? categoryName(t, row.category) : t("stats.uncategorized"),
                    value: row.bytes,
                  }))}
                />
              </Card>
            ) : null}
            <Card title={t("stats.by_status")}>
              <ul className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
                {Object.entries(data.by_status).map(([name, count]) => (
                  <li key={name}>
                    {t(`status.${name}` as Key)}: <span className="tabular-nums">{count}</span>
                  </li>
                ))}
              </ul>
            </Card>
          </div>
        ) : stats.error ? (
          <p className="p-6 text-sm text-danger">{stats.error.message}</p>
        ) : null}
      </main>
    </div>
  );
}
