import createClient from "openapi-fetch";
import type { Connection } from "./connection";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Download = Schemas["DownloadOut"];
export type Probe = Schemas["ProbeOut"];
export type Info = Schemas["InfoOut"];
export type Category = Schemas["Category"];
export type AddDownload = Schemas["AddDownload"];
export type Status = Download["status"];

export type Api = ReturnType<typeof makeApi>;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** The daemon's message for the person, never the raw body. */
function detail(error: unknown, status: number): string {
  if (error && typeof error === "object" && "detail" in error) {
    const value = (error as { detail: unknown }).detail;
    if (typeof value === "string") return value;
    if (Array.isArray(value) && value[0] && typeof value[0] === "object" && "msg" in value[0]) {
      return String((value[0] as { msg: unknown }).msg);
    }
  }
  return `sayld answered ${status}`;
}

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok || error !== undefined) throw new ApiError(detail(error, response.status), response.status);
  return data as T;
}

export function makeApi(connection: Connection, fetcher: typeof fetch = globalThis.fetch) {
  const client = createClient<paths>({
    baseUrl: connection.baseUrl,
    headers: { Authorization: `Bearer ${connection.token}` },
    fetch: (request) => fetcher(request),
  });
  const id = (download: string) => ({ params: { path: { download_id: download } } });
  return {
    info: () => unwrap(client.GET("/info")),
    downloads: () => unwrap(client.GET("/downloads")),
    download: (download: string) => unwrap(client.GET("/downloads/{download_id}", id(download))),
    add: (body: AddDownload) => unwrap(client.POST("/downloads", { body })),
    probe: (url: string) => unwrap(client.POST("/probe", { body: { url, headers: {} } })),
    pause: (download: string) => unwrap(client.POST("/downloads/{download_id}/pause", id(download))),
    resume: (download: string) =>
      unwrap(client.POST("/downloads/{download_id}/resume", id(download))),
    remove: (download: string) => unwrap(client.DELETE("/downloads/{download_id}", id(download))),
    patch: (download: string, body: Schemas["PatchDownload"]) =>
      unwrap(client.PATCH("/downloads/{download_id}", { ...id(download), body })),
    refresh: (download: string, url: string) =>
      unwrap(
        client.POST("/downloads/{download_id}/refresh-url", {
          ...id(download),
          body: { url, headers: {} },
        }),
      ),
    settings: () => unwrap(client.GET("/settings")) as Promise<Record<string, unknown>>,
    putSettings: (values: Record<string, unknown>) =>
      unwrap(client.PUT("/settings", { body: values })) as Promise<Record<string, unknown>>,
    categories: () => unwrap(client.GET("/categories")),
    addCategory: (body: Category) => unwrap(client.POST("/categories", { body })),
    removeCategory: (key: string) =>
      unwrap(client.DELETE("/categories/{key}", { params: { path: { key } } })),
  };
}
