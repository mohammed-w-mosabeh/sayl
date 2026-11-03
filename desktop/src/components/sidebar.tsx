"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Download as DownloadIcon,
  Folder,
  Inbox,
  Link2Off,
  PauseCircle,
  type LucideIcon,
} from "lucide-react";
import { useApi } from "@/components/providers";
import type { Download } from "@/lib/api/client";
import { useI18n, type Key } from "@/lib/i18n";
import { useUi, type Filter } from "@/lib/prefs";
import { counts, FILTER_ORDER } from "@/lib/status";
import { cn } from "@/lib/utils";

const ICONS: Record<Filter, LucideIcon> = {
  all: Inbox,
  active: DownloadIcon,
  queued: Clock,
  paused: PauseCircle,
  completed: CheckCircle2,
  failed: AlertTriangle,
  needs_refresh: Link2Off,
};

function Item({
  icon: Icon,
  label,
  count,
  active,
  onClick,
}: {
  icon: LucideIcon;
  label: string;
  count?: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-start text-sm",
        active ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:bg-muted",
      )}
    >
      <Icon className="size-4 shrink-0" />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {count ? <span className="text-xs tabular-nums">{count}</span> : null}
    </button>
  );
}

export function Sidebar({ rows }: { rows: Download[] }) {
  const { t } = useI18n();
  const api = useApi();
  const { filter, setFilter, category, setCategory } = useUi();
  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const totals = counts(rows);

  return (
    <nav className="flex w-52 shrink-0 flex-col gap-4 overflow-y-auto border-e bg-card/50 p-3">
      <div className="grid gap-0.5">
        {FILTER_ORDER.map((name) => (
          <Item
            key={name}
            icon={ICONS[name]}
            label={t(`filter.${name}` as Key)}
            count={name === "all" ? undefined : totals[name]}
            active={category === null && filter === name}
            onClick={() => setFilter(name)}
          />
        ))}
      </div>
      {categories.data?.length ? (
        <div className="grid gap-0.5">
          <h2 className="px-2.5 pb-1 text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {t("filter.categories")}
          </h2>
          {categories.data.map((item) => (
            <Item
              key={item.id}
              icon={Folder}
              label={item.name}
              count={rows.filter((row) => row.category_id === item.id).length}
              active={category === item.id}
              onClick={() => setCategory(item.id ?? null)}
            />
          ))}
        </div>
      ) : null}
    </nav>
  );
}
