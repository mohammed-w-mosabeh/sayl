//! The Python runtime that packaged builds carry in /usr/lib/sayl/runtime (docs/SPEC.md,
//! Packaging). The shell uses it to run `Sayl.AppImage sayl ...` and `Sayl.AppImage sayld`, and
//! to start sayld through `sayl service install` when nothing answers on its port.

use std::env;
use std::net::{SocketAddr, TcpStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::Duration;

/// `<exe dir>/../lib/sayl/runtime/bin/python3`: /usr/bin in the .deb, $APPDIR/usr/bin in the
/// AppImage. None in a development build.
pub fn python_for(exe: &Path) -> Option<PathBuf> {
    let python = exe.parent()?.parent()?.join("lib/sayl/runtime/bin/python3");
    python.is_file().then_some(python)
}

fn python() -> Option<PathBuf> {
    env::current_exe().ok().and_then(|exe| python_for(&exe))
}

/// Set by the AppImage runtime, and needed by `sayl service install` to find the AppImage.
const KEEP: [&str; 4] = ["APPDIR", "APPIMAGE", "ARGV0", "OWD"];

/// AppRun prepends the AppImage's directories to PATH, LD_LIBRARY_PATH, XDG_DATA_DIRS, and
/// more, and sets PYTHONHOME. Host programs that sayld starts (aria2c, ffmpeg, certutil) must
/// not load the AppImage's libraries, so every entry under `appdir` goes: a variable keeps its
/// other entries (Some) or is removed (None).
pub fn scrub(vars: &[(String, String)], appdir: &str) -> Vec<(String, Option<String>)> {
    let appdir = appdir.trim_end_matches('/');
    let mut changes = Vec::new();
    if appdir.is_empty() {
        return changes;
    }
    for (key, value) in vars {
        if KEEP.contains(&key.as_str()) || !value.contains(appdir) {
            continue;
        }
        let kept: Vec<&str> = value
            .split(':')
            .filter(|part| !part.is_empty() && !part.starts_with(appdir))
            .collect();
        changes.push((key.clone(), (!kept.is_empty()).then(|| kept.join(":"))));
    }
    changes
}

/// The bundled Python, with the AppImage's own paths taken out of its environment.
fn host_python(python: PathBuf) -> Command {
    let mut command = Command::new(python);
    command.env_remove("PYTHONHOME").env_remove("PYTHONPATH");
    let appdir = env::var("APPDIR").unwrap_or_default();
    let vars: Vec<(String, String)> = env::vars_os()
        .filter_map(|(key, value)| Some((key.into_string().ok()?, value.into_string().ok()?)))
        .collect();
    for (key, value) in scrub(&vars, &appdir) {
        match value {
            Some(value) => command.env(key, value),
            None => command.env_remove(key),
        };
    }
    command
}

/// The Python module behind a first argument of `sayl` or `sayld`.
pub fn module_for(command: &str) -> Option<&'static str> {
    match command {
        "sayl" => Some("cli.main"),
        "sayld" => Some("daemon.launch"),
        _ => None,
    }
}

/// Become the bundled CLI or daemon when the first argument names one; otherwise return.
pub fn dispatch() {
    let mut args = env::args_os().skip(1);
    let first = args.next();
    let Some(module) = first
        .as_ref()
        .and_then(|arg| arg.to_str())
        .and_then(module_for)
    else {
        return;
    };
    let Some(python) = python() else {
        eprintln!("this build of Sayl carries no Python runtime");
        std::process::exit(127);
    };
    let error = host_python(python)
        .args(["-I", "-m", module])
        .args(args)
        .exec();
    eprintln!("cannot start {module}: {error}");
    std::process::exit(126);
}

fn reachable(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&address, Duration::from_millis(300)).is_ok()
}

/// If sayld does not answer, enable and start its user unit with the bundled CLI, in the
/// background. A development build starts nothing.
pub fn ensure_daemon(port: u16) {
    let Some(python) = python() else {
        return;
    };
    thread::spawn(move || {
        if reachable(port) {
            return;
        }
        // Its output goes to the shell's stderr, which the session journal keeps.
        let _ = host_python(python)
            .args(["-I", "-m", "cli.main", "service", "install"])
            .stdin(Stdio::null())
            .status();
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn modules() {
        assert_eq!(module_for("sayl"), Some("cli.main"));
        assert_eq!(module_for("sayld"), Some("daemon.launch"));
        assert_eq!(module_for("--help"), None);
    }

    #[test]
    fn finds_the_runtime_beside_the_binary() {
        let root = env::temp_dir().join(format!("sayl-runtime-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let exe = root.join("usr/bin/sayl-desktop");
        assert_eq!(python_for(&exe), None);
        let python = root.join("usr/lib/sayl/runtime/bin/python3");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "").unwrap();
        assert_eq!(python_for(&exe), Some(python));
        let _ = fs::remove_dir_all(&root);
    }

    fn pair(key: &str, value: &str) -> (String, String) {
        (key.to_string(), value.to_string())
    }

    #[test]
    fn scrubs_appimage_paths() {
        let vars = vec![
            pair("PATH", "/tmp/.mount_x/usr/bin:/usr/bin:/bin"),
            pair("LD_LIBRARY_PATH", "/tmp/.mount_x/usr/lib"),
            pair("PYTHONHOME", "/tmp/.mount_x/usr/"),
            pair("APPIMAGE", "/home/me/Sayl.AppImage"),
            pair("APPDIR", "/tmp/.mount_x"),
            pair("HOME", "/home/me"),
        ];
        let changes = scrub(&vars, "/tmp/.mount_x/");
        let path = Some("/usr/bin:/bin".to_string());
        assert_eq!(changes.len(), 3);
        assert_eq!(changes[0], ("PATH".to_string(), path));
        assert_eq!(changes[1], ("LD_LIBRARY_PATH".to_string(), None));
        assert_eq!(changes[2], ("PYTHONHOME".to_string(), None));
        assert!(scrub(&vars, "").is_empty());
    }

    #[test]
    fn closed_port_is_not_reachable() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(reachable(port));
        drop(listener);
        assert!(!reachable(port));
    }
}
