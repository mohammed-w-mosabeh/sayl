/** Thin wrappers over Tauri. Each one does nothing outside the desktop app. */

import { inTauri } from "@/lib/api/connection";

export async function notify(title: string, body: string): Promise<void> {
  if (!inTauri()) return;
  const notification = await import("@tauri-apps/plugin-notification");
  let granted = await notification.isPermissionGranted();
  if (!granted) granted = (await notification.requestPermission()) === "granted";
  if (granted) notification.sendNotification({ title, body });
}

export async function pickFolder(current: string): Promise<string | null> {
  if (!inTauri()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const chosen = await open({ directory: true, multiple: false, defaultPath: current || undefined });
  return typeof chosen === "string" ? chosen : null;
}

async function invoke<T>(command: string, args?: Record<string, unknown>): Promise<T | null> {
  if (!inTauri()) return null;
  const core = await import("@tauri-apps/api/core");
  return core.invoke<T>(command, args);
}

export async function openFolder(path: string): Promise<void> {
  await invoke("open_folder", { path });
}

export async function setDropTarget(visible: boolean): Promise<void> {
  await invoke("set_drop_target", { visible });
}

export async function dropTargetVisible(): Promise<boolean> {
  return (await invoke<boolean>("drop_target_visible")) ?? false;
}

/** The tray menu is built in Rust, so it gets its labels from here when the language changes. */
export async function setTrayLabels(labels: { show: string; drop: string; quit: string }) {
  await invoke("set_tray_labels", { labels });
}

export async function showMainWindow(): Promise<void> {
  await invoke("show_main_window");
}
