"use client";

import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { DaemonScreen } from "@/components/daemon-screen";
import { ApiError, makeApi, type Api, type Download } from "@/lib/api/client";
import { loadConnection, type Connection, type ConnectionResult } from "@/lib/api/connection";
import { DOWNLOADS, isMediaReport, NOTIFY, useDaemonEvents, type DaemonEvent } from "@/lib/events";
import { I18nProvider, useI18n } from "@/lib/i18n";
import { notify, openVideoBar, setTrayLabels } from "@/lib/native";
import { normalizePrefs, useUi, type Theme } from "@/lib/prefs";

interface ApiContextValue {
  api: Api;
  connection: Connection;
}

const ApiContext = createContext<ApiContextValue | null>(null);

/** Supplies a ready API without the daemon gate; tests use it with a fake fetch. */
export function ApiProvider({ connection, api, children }: ApiContextValue & { children: ReactNode }) {
  return <ApiContext.Provider value={{ api, connection }}>{children}</ApiContext.Provider>;
}

export function useApi(): Api {
  const value = useContext(ApiContext);
  if (!value) throw new Error("useApi outside the daemon gate");
  return value.api;
}

function useTheme(theme: Theme): void {
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && media.matches);
      document.documentElement.classList.toggle("dark", dark);
      document.documentElement.style.colorScheme = dark ? "dark" : "light";
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
}

/** Appearance lives in the daemon's `ui` setting, so every window and reinstall agrees. */
function usePrefsSync(api: Api, primary: boolean): void {
  const prefs = useUi((state) => state.prefs);
  const setPrefs = useUi((state) => state.setPrefs);
  const [adopted, setAdopted] = useState(false);
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });

  // Once per connection: a webview with no cached copy (first run, new profile) takes the
  // daemon's. After that, this window's changes are written back.
  useEffect(() => {
    let current = true;
    void api
      .settings()
      .then((data) => {
        if (!current) return;
        if (!useUi.getState().prefsCached && data.ui) setPrefs(normalizePrefs(data.ui));
        setAdopted(true);
      })
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [api, setPrefs]);

  useEffect(() => {
    if (!primary || !adopted) return;
    const stored = normalizePrefs(settings.data?.ui);
    if (JSON.stringify(stored) === JSON.stringify(prefs)) return;
    void api.putSettings({ ui: prefs });
  }, [adopted, api, prefs, primary, settings.data?.ui]);
}

function Live({ primary, children }: { primary: boolean; children: ReactNode }) {
  const value = useContext(ApiContext) as ApiContextValue;
  const client = useQueryClient();
  const i18n = useI18n();
  const notifications = useUi((state) => state.prefs.notifications);
  const videoBar = useUi((state) => state.prefs.videoBar);
  usePrefsSync(value.api, primary);

  useEffect(() => {
    if (!primary) return;
    void setTrayLabels({
      show: i18n.t("tray.show"),
      drop: i18n.t("tray.drop_target"),
      quit: i18n.t("tray.quit"),
    }).catch(() => undefined);
  }, [i18n, primary]);

  const onEvent = useCallback(
    (event: DaemonEvent) => {
      if (isMediaReport(event)) {
        // The bar's own window decides whether to show: it may be dismissed or protected.
        if (primary && videoBar) void openVideoBar().catch(() => undefined);
        return;
      }
      const title = NOTIFY[event.event];
      if (!primary || !notifications || !title) return;
      const row = client.getQueryData<Download[]>(DOWNLOADS)?.find((item) => item.id === event.id);
      const name = row?.filename || (typeof event.filename === "string" ? event.filename : "");
      void notify(i18n.t(title), name).catch(() => undefined);
    },
    [client, i18n, notifications, primary, videoBar],
  );
  useDaemonEvents(value.connection, onEvent);
  return children;
}

function Gate({ primary, children }: { primary: boolean; children: ReactNode }) {
  const [result, setResult] = useState<ConnectionResult | null>(null);
  const [failure, setFailure] = useState("");

  const connect = useCallback(() => {
    loadConnection()
      .then(setResult)
      .catch((error: unknown) => setFailure(String(error)));
  }, []);
  const load = useCallback(() => {
    setFailure("");
    setResult(null);
    connect();
  }, [connect]);
  useEffect(connect, [connect]);

  const connection = result?.kind === "ready" ? result.connection : null;
  const api = useMemo(() => (connection ? makeApi(connection) : null), [connection]);
  const info = useQuery({
    queryKey: ["info", connection?.baseUrl],
    queryFn: () => (api as Api).info(),
    enabled: !!api,
    retry: false,
    refetchInterval: (query) => (query.state.status === "error" ? 3000 : false),
  });

  if (failure) return <DaemonScreen state="offline" onRetry={load} />;
  if (!result) return <DaemonScreen state="connecting" />;
  if (result.kind === "no_token") return <DaemonScreen state="no_token" onRetry={load} />;
  if (result.kind === "dev") return <DaemonScreen state="dev" onConnected={setResult} />;
  if (info.isPending) return <DaemonScreen state="connecting" />;
  if (info.isError) {
    const unauthorized = info.error instanceof ApiError && info.error.status === 401;
    return <DaemonScreen state={unauthorized ? "no_token" : "offline"} onRetry={load} />;
  }
  return (
    <ApiContext.Provider value={{ api: api as Api, connection: connection as Connection }}>
      <Live primary={primary}>{children}</Live>
    </ApiContext.Provider>
  );
}

/** `primary` is the main window: only it notifies, owns the tray, and saves preferences. */
export function Providers({ children, primary = true }: { children: ReactNode; primary?: boolean }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { staleTime: 2000, refetchOnWindowFocus: false } },
      }),
  );
  const prefs = useUi((state) => state.prefs);
  const bootPrefs = useUi((state) => state.bootPrefs);
  const booted = useUi((state) => state.prefsLoaded);
  useEffect(bootPrefs, [bootPrefs]);
  useTheme(prefs.theme);

  // Another window changed a preference: follow it.
  useEffect(() => {
    const follow = (event: StorageEvent) => {
      if (event.key === "sayl.prefs") bootPrefs();
    };
    window.addEventListener("storage", follow);
    return () => window.removeEventListener("storage", follow);
  }, [bootPrefs]);

  if (!booted) return null;
  return (
    <QueryClientProvider client={client}>
      <I18nProvider language={prefs.language}>
        <Gate primary={primary}>{children}</Gate>
      </I18nProvider>
    </QueryClientProvider>
  );
}
