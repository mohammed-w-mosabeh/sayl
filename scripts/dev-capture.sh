#!/usr/bin/env bash
# Run sayld from this checkout, with capture in PAC mode, in place of the installed v1.0.0
# package (which still uses manual proxy mode and breaks terminal tools such as Claude Code).
#
#   scripts/dev-capture.sh start    stop the installed service, start sayld from source,
#                                   enable capture, and verify it leaves terminals alone
#   scripts/dev-capture.sh check    run the checks again
#   scripts/dev-capture.sh stop     disable capture, stop the source sayld, restore the proxy
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/sayl"
PIDFILE="$STATE/dev-sayld.pid"
LOG="$STATE/dev-sayld.log"
cd "$REPO"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILED=1; }

daemon_up() { uv run --quiet sayl capture status >/dev/null 2>&1; }

source_pid() {
    [[ -f "$PIDFILE" ]] || return 1
    local pid
    pid="$(cat "$PIDFILE")"
    kill -0 "$pid" 2>/dev/null && echo "$pid"
}

check() {
    FAILED=0
    say "Checking"
    local mode url
    mode="$(gsettings get org.gnome.system.proxy mode 2>/dev/null || echo unknown)"
    url="$(gsettings get org.gnome.system.proxy autoconfig-url 2>/dev/null || echo)"
    if [[ "$mode" == "'auto'" ]]; then ok "GNOME proxy mode is auto (PAC: $url)"
    else bad "GNOME proxy mode is $mode, expected 'auto'"; fi

    # What a newly opened GNOME Terminal would export: nothing, in auto mode.
    local http_host
    http_host="$(gsettings get org.gnome.system.proxy.http host 2>/dev/null || echo)"
    if [[ "$mode" != "'manual'" ]]; then ok "new terminals get no http_proxy/https_proxy"
    else bad "manual mode: new terminals will export a proxy ($http_host)"; fi

    if env | grep -qiE '^(https?|all)_proxy=.*127\.0\.0\.1'; then
        bad "this shell has a proxy variable pointing at 127.0.0.1; open a new terminal"
    else ok "this shell does not route through Sayl"; fi

    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 https://api.anthropic.com/ 2>&1 || true)"
    if [[ "$code" =~ ^[0-9]{3}$ && "$code" != "000" ]]; then
        ok "api.anthropic.com reachable from the terminal (HTTP $code, certificate verified)"
    else bad "api.anthropic.com unreachable from the terminal: $code"; fi

    say "sayl doctor"
    if uv run --quiet sayl doctor; then ok "doctor reports no failures"
    else bad "doctor reports failures (see the table above)"; fi

    if [[ "$FAILED" == 0 ]]; then
        say "All good: browsers are captured, terminals and Claude Code go direct."
    else
        say "Something failed. Undo everything with: scripts/dev-capture.sh stop"
        return 1
    fi
}

start() {
    mkdir -p "$STATE"
    say "Stopping the installed sayld service (v1.0.0 has the old proxy behaviour)"
    if systemctl --user is-enabled --quiet sayld 2>/dev/null \
        || systemctl --user is-active --quiet sayld 2>/dev/null; then
        systemctl --user disable --now sayld
        ok "installed service stopped and disabled"
    else ok "installed service was not running"; fi

    if pid="$(source_pid)"; then
        ok "source sayld already running (pid $pid)"
    else
        if daemon_up; then
            echo "Another sayld (not from this script) is answering. Stop it first:" >&2
            pgrep -af sayld >&2 || true
            exit 1
        fi
        say "Installing dependencies and starting sayld from $REPO"
        uv sync --quiet
        nohup uv run sayld >>"$LOG" 2>&1 &
        echo $! >"$PIDFILE"
        for _ in $(seq 1 60); do
            daemon_up && break
            kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { echo "sayld exited; see $LOG" >&2; tail -20 "$LOG" >&2; exit 1; }
            sleep 0.5
        done
        daemon_up || { echo "sayld did not come up in 30 s; see $LOG" >&2; exit 1; }
        ok "sayld running (pid $(cat "$PIDFILE"), log $LOG)"
    fi

    say "Enabling capture"
    uv run --quiet sayl capture enable
    check
}

stop() {
    say "Disabling capture"
    if daemon_up; then uv run --quiet sayl capture disable || true; fi
    if pid="$(source_pid)"; then
        say "Stopping source sayld (pid $pid)"
        kill -TERM "$pid"
        for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
    fi
    rm -f "$PIDFILE"
    say "Restoring the desktop proxy setting, in case anything was left"
    uv run --quiet sayld --restore-proxy || true
    ok "GNOME proxy mode: $(gsettings get org.gnome.system.proxy mode 2>/dev/null || echo unknown)"
    echo "The installed v1.0.0 service stays disabled; re-enabling it brings back the old"
    echo "manual-proxy behaviour. Install v1.0.1 once it is released."
}

case "${1:-}" in
    start) start ;;
    check) check ;;
    stop) stop ;;
    *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
