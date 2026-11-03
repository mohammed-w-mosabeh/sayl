/** Where sayld listens and its bearer token. The token never leaves this machine. */

export interface Connection {
  baseUrl: string;
  token: string;
}

export type ConnectionResult =
  | { kind: "ready"; connection: Connection }
  | { kind: "no_token"; port: number }
  | { kind: "dev" };

const DEV_KEY = "sayl.dev-connection";

export function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

interface NativeConnection {
  port: number;
  token: string | null;
}

/**
 * In the app, Rust reads ~/.config/sayl/token and config.toml. In a plain browser (`pnpm dev`),
 * the token is pasted once and kept in sessionStorage, for that tab only.
 */
export async function loadConnection(): Promise<ConnectionResult> {
  if (inTauri()) {
    const { invoke } = await import("@tauri-apps/api/core");
    const native = await invoke<NativeConnection>("connection");
    if (!native.token) return { kind: "no_token", port: native.port };
    return { kind: "ready", connection: { baseUrl: baseUrl(native.port), token: native.token } };
  }
  try {
    const raw = sessionStorage.getItem(DEV_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Connection;
      if (parsed.baseUrl && parsed.token) return { kind: "ready", connection: parsed };
    }
  } catch {
    // Fall through to the connect form.
  }
  return { kind: "dev" };
}

export function saveDevConnection(port: number, token: string): Connection {
  const connection = { baseUrl: baseUrl(port), token: token.trim() };
  try {
    sessionStorage.setItem(DEV_KEY, JSON.stringify(connection));
  } catch {
    // The connection still works for this page load.
  }
  return connection;
}

export function baseUrl(port: number): string {
  return `http://127.0.0.1:${port}`;
}

export function socketUrl(connection: Connection): string {
  return `${connection.baseUrl.replace(/^http/, "ws")}/ws`;
}
