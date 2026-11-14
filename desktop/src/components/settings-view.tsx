"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { inTauri } from "@/lib/api/connection";
import { languages, useI18n, type Key, type Language } from "@/lib/i18n";
import { dropTargetVisible, setDropTarget } from "@/lib/native";
import { useUi, type Theme } from "@/lib/prefs";

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

  return (
    <Section title={t("settings.categories")} body={t("settings.categories.body")}>
      {categories.data?.length ? (
        <ul className="divide-y rounded-md border">
          {categories.data.map((item) => (
            <li key={item.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span dir="auto" className="w-32 truncate font-medium">
                {item.name}
              </span>
              <bdi dir="ltr" className="min-w-0 flex-1 truncate text-muted-foreground">
                {item.extensions}
              </bdi>
              <bdi dir="ltr" className="hidden min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground md:block">
                {item.dest_dir}
              </bdi>
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

          <Clipboard />

          <Categories />

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
