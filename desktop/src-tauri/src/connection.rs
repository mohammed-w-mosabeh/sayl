//! Finds sayld the way the CLI does: the port in config.toml and the token file beside it.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const DEFAULT_PORT: u16 = 6971;

#[derive(Debug, PartialEq, Serialize)]
pub struct Native {
    pub port: u16,
    /// None until sayld has run once and written ~/.config/sayl/token.
    pub token: Option<String>,
}

#[derive(Deserialize)]
struct Config {
    port: Option<u16>,
}

/// `$XDG_CONFIG_HOME/sayl`, or `~/.config/sayl`, as in daemon/config.py.
pub fn config_dir() -> PathBuf {
    let base = env::var_os("XDG_CONFIG_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
        .unwrap_or_else(|| PathBuf::from(".config"));
    base.join("sayl")
}

pub fn read(dir: &Path) -> Native {
    let port = fs::read_to_string(dir.join("config.toml"))
        .ok()
        .and_then(|text| toml::from_str::<Config>(&text).ok())
        .and_then(|config| config.port)
        .unwrap_or(DEFAULT_PORT);
    let token = fs::read_to_string(dir.join("token"))
        .ok()
        .map(|text| text.trim().to_owned())
        .filter(|token| !token.is_empty());
    Native { port, token }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = env::temp_dir().join(format!("sayl-connection-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn defaults_without_files() {
        let dir = scratch("empty");
        assert_eq!(
            read(&dir),
            Native {
                port: DEFAULT_PORT,
                token: None
            }
        );
    }

    #[test]
    fn reads_port_and_trims_token() {
        let dir = scratch("full");
        fs::write(dir.join("config.toml"), "port = 7000\nfuture = true\n").unwrap();
        fs::write(dir.join("token"), "abc\n").unwrap();
        assert_eq!(
            read(&dir),
            Native {
                port: 7000,
                token: Some("abc".into())
            }
        );
    }

    #[test]
    fn broken_config_falls_back_to_the_default_port() {
        let dir = scratch("broken");
        fs::write(dir.join("config.toml"), "port = \"x\"").unwrap();
        assert_eq!(read(&dir).port, DEFAULT_PORT);
    }
}
