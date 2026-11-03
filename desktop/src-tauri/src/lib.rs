//! The Sayl desktop shell. It holds no download state: sayld owns that, and the webview talks
//! to it directly over HTTP and WebSocket on 127.0.0.1. Rust only provides what a webview
//! cannot: the token on disk, the tray, the floating drop target, and opening folders.

mod connection;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, Wry};

const MAIN: &str = "main";
const DROP: &str = "drop";
const DROP_SIZE: f64 = 112.0;

struct Tray {
    show: MenuItem<Wry>,
    drop: CheckMenuItem<Wry>,
    quit: MenuItem<Wry>,
}

#[derive(Default, Serialize, Deserialize)]
struct ShellState {
    drop_target: bool,
}

/// Window preferences that only the shell needs. Download state never lives here.
fn state_file(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join("shell.json"))
}

fn load_state(app: &AppHandle) -> ShellState {
    state_file(app)
        .and_then(|file| fs::read_to_string(file).ok())
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn save_state(app: &AppHandle, state: &ShellState) {
    let Some(file) = state_file(app) else { return };
    if let Some(dir) = file.parent() {
        let _ = fs::create_dir_all(dir);
    }
    if let Ok(text) = serde_json::to_string(state) {
        let _ = fs::write(file, text);
    }
}

fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn drop_window(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    if let Some(window) = app.get_webview_window(DROP) {
        return Ok(window);
    }
    // Native drag-and-drop is off so the page receives the browser's text/uri-list.
    WebviewWindowBuilder::new(app, DROP, WebviewUrl::App("drop/".into()))
        .title("Sayl")
        .inner_size(DROP_SIZE, DROP_SIZE)
        .resizable(false)
        .decorations(false)
        .transparent(true)
        .always_on_top(true)
        .skip_taskbar(true)
        .visible(false)
        .disable_drag_drop_handler()
        .build()
}

fn apply_drop_target(app: &AppHandle, visible: bool) -> tauri::Result<()> {
    let window = drop_window(app)?;
    if visible {
        window.show()?;
    } else {
        window.hide()?;
    }
    if let Some(tray) = app.try_state::<Tray>() {
        let _ = tray.drop.set_checked(visible);
    }
    save_state(
        app,
        &ShellState {
            drop_target: visible,
        },
    );
    Ok(())
}

#[tauri::command]
fn connection() -> connection::Native {
    connection::read(&connection::config_dir())
}

#[tauri::command]
fn set_drop_target(app: AppHandle, visible: bool) -> Result<(), String> {
    apply_drop_target(&app, visible).map_err(|error| error.to_string())
}

#[tauri::command]
fn drop_target_visible(app: AppHandle) -> bool {
    app.get_webview_window(DROP)
        .and_then(|window| window.is_visible().ok())
        .unwrap_or(false)
}

#[tauri::command]
fn show_main_window(app: AppHandle) {
    show_main(&app);
}

#[derive(Deserialize)]
struct Labels {
    show: String,
    drop: String,
    quit: String,
}

#[tauri::command]
fn set_tray_labels(app: AppHandle, labels: Labels) -> Result<(), String> {
    let tray = app.try_state::<Tray>().ok_or("no tray")?;
    let set = |result: tauri::Result<()>| result.map_err(|error| error.to_string());
    set(tray.show.set_text(labels.show))?;
    set(tray.drop.set_text(labels.drop))?;
    set(tray.quit.set_text(labels.quit))
}

/// Opens a download folder in the file manager. Only existing directories, never files, so a
/// crafted path cannot launch something.
#[tauri::command]
fn open_folder(path: String) -> Result<(), String> {
    let folder = Path::new(&path);
    if !folder.is_absolute() || !folder.is_dir() {
        return Err("not a folder".into());
    }
    Command::new("xdg-open")
        .arg(folder)
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show Sayl", true, None::<&str>)?;
    let drop = CheckMenuItem::with_id(
        app,
        "drop",
        "Floating drop target",
        true,
        false,
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, "quit", "Quit Sayl", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&show, &drop, &separator, &quit])?;

    let mut builder = TrayIconBuilder::with_id("sayl")
        .tooltip("Sayl")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main(app),
            "drop" => {
                let visible = !drop_target_visible(app.clone());
                let _ = apply_drop_target(app, visible);
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    app.manage(Tray { show, drop, quit });
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main(app)
        }))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            connection,
            set_drop_target,
            drop_target_visible,
            show_main_window,
            set_tray_labels,
            open_folder
        ])
        .setup(|app| {
            let handle = app.handle();
            build_tray(handle)?;
            if load_state(handle).drop_target {
                apply_drop_target(handle, true)?;
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the main window keeps the tray; downloads run in sayld either way.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == MAIN {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Sayl");
}
