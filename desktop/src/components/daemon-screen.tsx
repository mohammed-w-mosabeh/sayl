"use client";

import { Loader2, PlugZap, Server } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";
import { saveDevConnection, type ConnectionResult } from "@/lib/api/connection";
import { useI18n } from "@/lib/i18n";

const DEFAULT_PORT = 6971;

type Props =
  | { state: "connecting" }
  | { state: "offline" | "no_token"; onRetry: () => void }
  | { state: "dev"; onConnected: (result: ConnectionResult) => void };

export function DaemonScreen(props: Props) {
  const { t } = useI18n();
  if (props.state === "connecting") {
    return (
      <main className="flex h-screen items-center justify-center gap-2 text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        {t("daemon.connecting")}
      </main>
    );
  }
  if (props.state === "dev") return <DevConnect onConnected={props.onConnected} />;
  const offline = props.state === "offline";
  return (
    <main className="flex h-screen items-center justify-center p-6">
      <div className="grid max-w-md gap-4 rounded-lg border bg-card p-6 text-center shadow-sm">
        <Server className="mx-auto size-8 text-muted-foreground" />
        <h1 className="text-lg font-semibold">
          {t(offline ? "daemon.offline.title" : "daemon.no_token.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {t(offline ? "daemon.offline.body" : "daemon.no_token.body")}
        </p>
        <code dir="ltr" className="rounded-md bg-muted px-3 py-2 font-mono text-sm">
          sayld
        </code>
        <Button variant="primary" onClick={props.onRetry}>
          {t("action.retry")}
        </Button>
      </div>
    </main>
  );
}

function DevConnect({ onConnected }: { onConnected: (result: ConnectionResult) => void }) {
  const { t } = useI18n();
  const [token, setToken] = useState("");
  const [port, setPort] = useState(String(DEFAULT_PORT));
  return (
    <main className="flex h-screen items-center justify-center p-6">
      <form
        className="grid w-full max-w-md gap-4 rounded-lg border bg-card p-6 shadow-sm"
        onSubmit={(event) => {
          event.preventDefault();
          const connection = saveDevConnection(Number(port) || DEFAULT_PORT, token);
          onConnected({ kind: "ready", connection });
        }}
      >
        <div className="flex items-center gap-2">
          <PlugZap className="size-5 text-primary" />
          <h1 className="text-lg font-semibold">{t("daemon.dev.title")}</h1>
        </div>
        <p className="text-sm text-muted-foreground">{t("daemon.dev.body")}</p>
        <Field label={t("daemon.dev.token")} htmlFor="token">
          <Input
            id="token"
            dir="ltr"
            type="password"
            autoComplete="off"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            required
          />
        </Field>
        <Field label={t("daemon.dev.port")} htmlFor="port">
          <Input
            id="port"
            dir="ltr"
            inputMode="numeric"
            value={port}
            onChange={(event) => setPort(event.target.value)}
          />
        </Field>
        <Button type="submit" variant="primary">
          {t("daemon.dev.connect")}
        </Button>
      </form>
    </main>
  );
}
