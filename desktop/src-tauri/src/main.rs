// Hides the console window on Windows release builds; harmless on Linux.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // `Sayl.AppImage sayl ...` and `Sayl.AppImage sayld` never reach Tauri.
    sayl_desktop_lib::runtime::dispatch();
    sayl_desktop_lib::run();
}
