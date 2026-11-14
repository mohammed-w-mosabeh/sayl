"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Play, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import type { Category, ScheduleIn } from "@/lib/api/client";
import { inTauri } from "@/lib/api/connection";
import { categoryName } from "@/lib/categories";
import { SCHEDULES } from "@/lib/events";
import { dateTime } from "@/lib/format";
import { languages, useI18n, type Key, type Language } from "@/lib/i18n";
import { dropTargetVisible, setDropTarget } from "@/lib/native";
import { useUi, type Theme } from "@/lib/prefs";
import { cronFor, type Days } from "@/lib/schedules";

function Section({ title, body, children }: { title: string; body?: string; children: ReactNode }) {
  return (
    <section className="grid gap-4 rounded-lg border bg-card p-5">
      <div className="grid gap-1">
        <h2 className="font-semibold">{title}</h2>
        {body ? <p className="text-sm text-muted-foreground">{body}</p> : null}
      </div>
      {children}
    </section>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex items-center justify-between gap-4 text-sm">
      {label}
      <Switch checked={checked} onCheckedChange={onChange} />
    </label>
  );
}

function SpeedLimit() {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [value, setValue] = useState<string | null>(null);
  const stored = typeof settings.data?.speed_limit === "number" ? settings.data.speed_limit : 0;
  const shown = value ?? String(Math.round(stored / 1024));
  const save = useMutation({
    mutationFn: () => api.putSettings({ speed_limit: Math.max(0, Math.round(Number(shown) || 0)) * 1024 }),
    onSuccess: (data) => {
      client.setQueryData(["settings"], data);
      setValue(null);
    },
  });
  return (
    <form
      className="grid grid-cols-[1fr_auto] items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <Field label={t("settings.speed_limit")} htmlFor="speed-limit">
        <Input
          id="speed-limit"
          dir="ltr"
          inputMode="numeric"
          value={shown}
          onChange={(event) => setValue(event.target.value)}
        />
      </Field>
      <Button type="submit" disabled={save.isPending}>
        {save.isSuccess && value === null ? <Check /> : null}
        {save.isSuccess && value === null ? t("settings.saved") : t("action.save")}
      </Button>
    </form>
  );
}

function HostPolicy() {
  const { t } = useI18n();
  const api = useApi();
  const clear = useMutation({ mutationFn: () => api.putSettings({ clear_host_policy: true }) });
  return (
    <div className="grid gap-2">
      <p className="text-sm font-medium">{t("settings.host_policy")}</p>
      <p className="text-sm text-muted-foreground">{t("settings.host_policy.body")}</p>
      <div>
        <Button size="sm" onClick={() => clear.mutate()} disabled={clear.isPending}>
          {clear.isSuccess ? <Check /> : null}
          {clear.isSuccess ? t("settings.host_policy.cleared") : t("settings.host_policy.clear")}
        </Button>
      </div>
    </div>
  );
}

function Categories() {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const categories = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const [draft, setDraft] = useState({ name: "", extensions: "", dest_dir: "" });
  const refresh = () => void client.invalidateQueries({ queryKey: ["categories"] });
  const add = useMutation({
    mutationFn: () =>
      api.addCategory({
        name: draft.name.trim(),
        extensions: draft.extensions
          .split(/[\s,\u060C]+/)
          .map((item) => item.replace(/^\./, "").toLowerCase())
          .filter(Boolean)
          .join(","),
        dest_dir: draft.dest_dir.trim(),
      }),
    onSuccess: () => {
      setDraft({ name: "", extensions: "", dest_dir: "" });
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: api.removeCategory, onSuccess: refresh });
  const move = useMutation({
    mutationFn: ({ item, dest }: { item: Category; dest: string }) =>
      api.replaceCategory(item.id ?? "", { ...item, dest_dir: dest }),
    onSuccess: refresh,
  });

  return (
    <Section title={t("settings.categories")} body={t("settings.categories.body")}>
      {categories.data?.length ? (
        <ul className="divide-y rounded-md border">
          {categories.data.map((item) => (
            <li key={item.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span dir="auto" className="w-32 truncate font-medium">
                {categoryName(t, item.name)}
              </span>
              <bdi dir="ltr" className="min-w-0 flex-1 truncate text-muted-foreground" title={item.extensions}>
                {item.extensions}
              </bdi>
              <Input
                dir="ltr"
                aria-label={t("settings.categories.dest")}
                placeholder={t("settings.categories.folder_default")}
                defaultValue={item.dest_dir}
                className="hidden h-8 min-w-0 flex-1 font-mono text-xs md:block"
                onBlur={(event) => {
                  const dest = event.target.value.trim();
                  if (dest !== (item.dest_dir ?? "")) move.mutate({ item, dest });
                }}
              />
              <Button
                size="icon"
                variant="ghost"
                aria-label={t("action.remove")}
                onClick={() => item.id && remove.mutate(item.id)}
              >
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{t("settings.categories.empty")}</p>
      )}
      <form
        className="grid gap-3 md:grid-cols-[1fr_1fr_1fr_auto] md:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          if (draft.name.trim()) add.mutate();
        }}
      >
        <Field label={t("settings.categories.name")} htmlFor="cat-name">
          <Input id="cat-name" dir="auto" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </Field>
        <Field label={t("settings.categories.extensions")} htmlFor="cat-ext">
          <Input
            id="cat-ext"
            dir="ltr"
            placeholder="iso, img"
            value={draft.extensions}
            onChange={(e) => setDraft({ ...draft, extensions: e.target.value })}
          />
        </Field>
        <Field label={t("settings.categories.dest")} htmlFor="cat-dest">
          <Input
            id="cat-dest"
            dir="ltr"
            value={draft.dest_dir}
            onChange={(e) => setDraft({ ...draft, dest_dir: e.target.value })}
          />
        </Field>
        <Button type="submit" disabled={!draft.name.trim() || add.isPending}>
          <Plus />
          {t("settings.categories.add")}
        </Button>
      </form>
      {add.error ? <p className="text-sm text-danger">{add.error.message}</p> : null}
    </Section>
  );
}

const EMPTY_SCHEDULE = {
  name: "",
  time: "02:00",
  days: "every" as Days,
  cron: "",
  action: "start_queue" as ScheduleIn["action"],
  limit: "0",
  after: "none" as NonNullable<ScheduleIn["post_action"]>,
  command: "",
};

function Schedules() {
  const i18n = useI18n();
  const { t } = i18n;
  const api = useApi();
  const client = useQueryClient();
  const schedules = useQuery({ queryKey: SCHEDULES, queryFn: api.schedules });
  const refresh = () => void client.invalidateQueries({ queryKey: SCHEDULES });
  const [draft, setDraft] = useState(EMPTY_SCHEDULE);
  const cron = cronFor(draft.time, draft.days, draft.cron);
  const body = (): ScheduleIn => ({
    name: draft.name.trim() || t(`schedule.${draft.action}`),
    cron,
    action: draft.action,
    params:
      draft.action === "speed_limit"
        ? { limit: Math.max(0, Math.round(Number(draft.limit) || 0)) * 1024 }
        : draft.after === "command"
          ? { command: draft.command.trim() }
          : {},
    post_action: draft.action === "start_queue" ? draft.after : "none",
    enabled: true,
  });
  const add = useMutation({
    mutationFn: () => api.addSchedule(body()),
    onSuccess: () => {
      setDraft(EMPTY_SCHEDULE);
      refresh();
    },
  });
  const toggle = useMutation({
    mutationFn: (item: ScheduleIn & { id: string }) => api.replaceSchedule(item.id, item),
    onSuccess: refresh,
  });
  const run = useMutation({ mutationFn: api.runSchedule });
  const remove = useMutation({ mutationFn: api.removeSchedule, onSuccess: refresh });
  const change = (values: Partial<typeof draft>) => setDraft({ ...draft, ...values });

  return (
    <Section title={t("settings.schedules")} body={t("settings.schedules.body")}>
      {schedules.data?.length ? (
        <ul className="divide-y rounded-md border">
          {schedules.data.map((item) => (
            <li key={item.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <Switch
                aria-label={item.name}
                checked={item.enabled}
                onCheckedChange={(enabled) => toggle.mutate({ ...item, enabled })}
              />
              <div className="grid min-w-0 flex-1 gap-0.5">
                <span dir="auto" className="truncate font-medium">
                  {item.name}
                </span>
                <span className="truncate text-xs text-muted-foreground">
                  {t(`schedule.${item.action}`)}
                  {item.post_action !== "none" ? ` · ${t(`post.${item.post_action}` as Key)}` : ""} ·{" "}
                  <bdi dir="ltr" className="font-mono">
                    {item.cron}
                  </bdi>{" "}
                  ·{" "}
                  {item.next_run
                    ? t("settings.schedules.next", { when: dateTime(i18n, item.next_run) })
                    : t("settings.schedules.off")}
                </span>
              </div>
              <Button
                size="icon"
                variant="ghost"
                aria-label={t("settings.schedules.run")}
                title={t("settings.schedules.run")}
                onClick={() => run.mutate(item.id)}
              >
                <Play />
              </Button>
              <Button size="icon" variant="ghost" aria-label={t("action.remove")} onClick={() => remove.mutate(item.id)}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{t("settings.schedules.empty")}</p>
      )}
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (cron) add.mutate();
        }}
      >
        <Field label={t("settings.schedules.name")} htmlFor="sched-name">
          <Input id="sched-name" dir="auto" value={draft.name} onChange={(e) => change({ name: e.target.value })} />
        </Field>
        <Field label={t("settings.schedules.action")} htmlFor="sched-action">
          <Select
            id="sched-action"
            value={draft.action}
            onChange={(e) => change({ action: e.target.value as ScheduleIn["action"] })}
          >
            {(["start_queue", "stop_queue", "speed_limit"] as const).map((action) => (
              <option key={action} value={action}>
                {t(`schedule.${action}`)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("settings.schedules.days")} htmlFor="sched-days">
          <Select id="sched-days" value={draft.days} onChange={(e) => change({ days: e.target.value as Days })}>
            {(["every", "mon_fri", "sun_thu", "custom"] as const).map((days) => (
              <option key={days} value={days}>
                {t(`settings.schedules.days.${days}`)}
              </option>
            ))}
          </Select>
        </Field>
        {draft.days === "custom" ? (
          <Field label={t("settings.schedules.cron")} htmlFor="sched-cron">
            <Input
              id="sched-cron"
              dir="ltr"
              className="font-mono"
              placeholder="0 2 * * *"
              value={draft.cron}
              onChange={(e) => change({ cron: e.target.value })}
            />
          </Field>
        ) : (
          <Field label={t("settings.schedules.time")} htmlFor="sched-time">
            <Input id="sched-time" type="time" dir="ltr" value={draft.time} onChange={(e) => change({ time: e.target.value })} />
          </Field>
        )}
        {draft.action === "speed_limit" ? (
          <Field label={t("settings.schedules.limit")} htmlFor="sched-limit">
            <Input id="sched-limit" dir="ltr" inputMode="numeric" value={draft.limit} onChange={(e) => change({ limit: e.target.value })} />
          </Field>
        ) : null}
        {draft.action === "start_queue" ? (
          <Field label={t("settings.schedules.after")} htmlFor="sched-after" hint={t("settings.schedules.post_hint")}>
            <Select
              id="sched-after"
              value={draft.after}
              onChange={(e) => change({ after: e.target.value as typeof draft.after })}
            >
              {(["none", "shutdown", "suspend", "command"] as const).map((post) => (
                <option key={post} value={post}>
                  {t(`post.${post}`)}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}
        {draft.action === "start_queue" && draft.after === "command" ? (
          <Field label={t("settings.schedules.command")} htmlFor="sched-command">
            <Input
              id="sched-command"
              dir="ltr"
              className="font-mono"
              placeholder="notify-send Done"
              value={draft.command}
              onChange={(e) => change({ command: e.target.value })}
            />
          </Field>
        ) : null}
        <div className="flex items-end sm:col-span-2">
          <Button type="submit" disabled={!cron || add.isPending}>
            <Plus />
            {t("settings.schedules.add")}
          </Button>
        </div>
      </form>
      {add.error ? <p className="text-sm text-danger">{add.error.message}</p> : null}
    </Section>
  );
}

function OutboundProxy() {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [value, setValue] = useState<string | null>(null);
  const stored = typeof settings.data?.outbound_proxy === "string" ? settings.data.outbound_proxy : "";
  const shown = value ?? stored;
  const save = useMutation({
    mutationFn: () => api.putSettings({ outbound_proxy: shown.trim() }),
    onSuccess: (data) => {
      client.setQueryData(["settings"], data);
      setValue(null);
    },
  });
  return (
    <Section title={t("settings.proxy")} body={t("settings.proxy.body")}>
      <form
        className="grid grid-cols-[1fr_auto] items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <Field label={t("settings.proxy.url")} htmlFor="proxy-url" hint={t("settings.proxy.hint")}>
          <Input
            id="proxy-url"
            dir="ltr"
            className="font-mono"
            autoComplete="off"
            spellCheck={false}
            value={shown}
            onChange={(event) => {
              setValue(event.target.value);
              save.reset();
            }}
          />
        </Field>
        <Button type="submit" disabled={value === null || save.isPending} className="mb-5">
          {save.isSuccess && value === null ? <Check /> : null}
          {save.isSuccess && value === null ? t("settings.saved") : t("action.save")}
        </Button>
      </form>
      {save.error ? <p className="text-sm text-danger">{save.error.message}</p> : null}
    </Section>
  );
}

export function ignoreList(text: string): string[] {
  return [...new Set(text.split(/[\n,\u060C]+/).map((line) => line.trim().toLowerCase()).filter(Boolean))];
}

function Clipboard() {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const info = useQuery({ queryKey: ["info"], queryFn: api.info, refetchInterval: 5000 });
  const [draft, setDraft] = useState<string | null>(null);
  const stored = Array.isArray(settings.data?.clipboard_ignore)
    ? (settings.data.clipboard_ignore as unknown[]).map(String)
    : [];
  const enabled = settings.data?.clipboard_enabled !== false;
  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.putSettings(values),
    onSuccess: (data) => {
      client.setQueryData(["settings"], data);
      void client.invalidateQueries({ queryKey: ["info"] });
      setDraft(null);
    },
  });
  const state = info.data?.clipboard ?? "off";
  const known = ["off", "unavailable", "wayland", "xwayland_poll", "x11", "poll"];

  return (
    <Section title={t("settings.clipboard")} body={t("settings.clipboard.body")}>
      <Toggle
        label={t("settings.clipboard.enabled")}
        checked={enabled}
        onChange={(checked) => save.mutate({ clipboard_enabled: checked })}
      />
      <p className="text-sm text-muted-foreground">
        {t("settings.clipboard.state", {
          state: known.includes(state) ? t(`settings.clipboard.state.${state}` as Key) : state,
        })}
      </p>
      <form
        className="grid gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate({ clipboard_ignore: ignoreList(draft ?? "") });
        }}
      >
        <Field label={t("settings.clipboard.ignore")} htmlFor="clip-ignore" hint={t("settings.clipboard.ignore.hint")}>
          <Textarea
            id="clip-ignore"
            dir="ltr"
            rows={4}
            value={draft ?? stored.join("\n")}
            onChange={(event) => setDraft(event.target.value)}
          />
        </Field>
        <div>
          <Button type="submit" size="sm" disabled={draft === null || save.isPending}>
            {t("action.save")}
          </Button>
        </div>
      </form>
    </Section>
  );
}

function DropTargetToggle() {
  const { t } = useI18n();
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    void dropTargetVisible().then(setVisible);
  }, []);
  if (!inTauri()) return null;
  return (
    <Toggle
      label={t("settings.drop_target")}
      checked={visible}
      onChange={(checked) => {
        setVisible(checked);
        void setDropTarget(checked);
      }}
    />
  );
}

export function SettingsView() {
  const { t } = useI18n();
  const api = useApi();
  const { prefs, setPrefs } = useUi();
  const info = useQuery({ queryKey: ["info"], queryFn: api.info });

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b bg-card px-4 py-2.5">
        <Button asChild size="icon" variant="ghost" aria-label={t("nav.back")} title={t("nav.back")}>
          <Link href="/">
            <ArrowLeft className="rtl:-scale-x-100" />
          </Link>
        </Button>
        <h1 className="font-semibold">{t("settings.title")}</h1>
      </header>
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto grid max-w-3xl gap-5 p-6">
          <Section title={t("settings.appearance")}>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("settings.language")} htmlFor="language">
                <Select
                  id="language"
                  value={prefs.language}
                  onChange={(event) => setPrefs({ language: event.target.value as Language })}
                >
                  {Object.entries(languages).map(([code, name]) => (
                    <option key={code} value={code}>
                      {name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={t("settings.theme")} htmlFor="theme">
                <Select
                  id="theme"
                  value={prefs.theme}
                  onChange={(event) => setPrefs({ theme: event.target.value as Theme })}
                >
                  <option value="system">{t("settings.theme.system")}</option>
                  <option value="light">{t("settings.theme.light")}</option>
                  <option value="dark">{t("settings.theme.dark")}</option>
                </Select>
              </Field>
            </div>
          </Section>

          <Section title={t("settings.downloads")}>
            <SpeedLimit />
            <Toggle
              label={t("settings.notifications")}
              checked={prefs.notifications}
              onChange={(notifications) => setPrefs({ notifications })}
            />
            <DropTargetToggle />
            <Toggle
              label={t("settings.video_bar")}
              checked={prefs.videoBar}
              onChange={(videoBar) => setPrefs({ videoBar })}
            />
            <HostPolicy />
          </Section>

          <Schedules />

          <Clipboard />

          <Categories />

          <OutboundProxy />

          {info.data ? (
            <Section title={t("settings.about")}>
              <div className="grid gap-1 text-sm text-muted-foreground">
                <p>{t("settings.about.version", { version: info.data.version })}</p>
                <p>
                  {t("settings.about.default_dir", { dir: "" })}
                  <bdi dir="ltr" className="font-mono">
                    {info.data.default_dir}
                  </bdi>
                </p>
              </div>
            </Section>
          ) : null}
        </div>
      </main>
    </div>
  );
}
