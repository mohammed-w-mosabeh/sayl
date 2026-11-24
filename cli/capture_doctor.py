"""Capture checks behind `sayl doctor`: listener, system proxy, CA, and a passthrough probe.

Read only. Nothing here changes a setting: the fixes are printed for the person to run.
"""

import os
import socket
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cli.doctor import Check, Status
from daemon.capture import ca
from daemon.capture.sysproxy import SystemProxy
from daemon.capture.trust import Trust
from daemon.config import Config, Paths

PROBE_HOST = "api.anthropic.com"
PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)
RESTORE = "sayld --restore-proxy"


@dataclass(frozen=True)
class Probe:
    """What a client saw for PROBE_HOST through the proxy."""

    issuer: str = ""
    error: str = ""


Listening = Callable[[int], bool]
Prober = Callable[[int, str], Probe]
GnomeMode = Callable[[], tuple[str, str] | None]


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _issuer_name(certificate: Mapping[str, object]) -> str:
    fields: dict[str, str] = {}
    for rdn in certificate.get("issuer", ()):  # type: ignore[attr-defined]
        for key, value in rdn:
            fields[key] = value
    return " / ".join(v for v in (fields.get("organizationName"), fields.get("commonName")) if v)


def probe(port: int, host: str = PROBE_HOST, timeout: float = 10) -> Probe:
    """CONNECT through the capture proxy, then verify TLS against the system's own roots.

    A passed-through host verifies and names its real issuer. An intercepted one fails with an
    unknown issuer, because the system bundle does not hold Sayl's CA, or names Sayl's CA if the
    person trusted it system-wide.
    """
    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError as error:
        return Probe(error=f"proxy not reachable: {error}")
    with raw:
        raw.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 8192:
            chunk = raw.recv(1024)
            if not chunk:
                break
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200" not in status:
            return Probe(error=f"proxy answered {status or 'nothing'}")
        context = ssl.create_default_context()
        try:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                certificate = tls.getpeercert() or {}
        except ssl.SSLCertVerificationError as error:
            return Probe(error=f"certificate rejected: {error.verify_message}")
        except (ssl.SSLError, OSError) as error:
            return Probe(error=f"TLS failed: {error}")
    return Probe(issuer=_issuer_name(certificate))


def _listener(config: Config, is_listening: Listening) -> Check:
    if is_listening(config.capture_port):
        return Check("capture listener", Status.OK, f"listening on 127.0.0.1:{config.capture_port}")
    return Check("capture listener", Status.OK, "not running (capture is off)")


def _system_proxy(
    config: Config, proxy: SystemProxy, gnome: GnomeMode, running: bool
) -> list[Check]:
    return [
        _proxy_setting(config, proxy, gnome, running),
        Check("redirect rules", Status.OK, "none: Sayl adds no nftables/iptables or hosts entries"),
    ]


def _proxy_setting(config: Config, proxy: SystemProxy, gnome: GnomeMode, running: bool) -> Check:
    name = "system proxy"
    if proxy.active() and not running:
        detail = "Sayl's setting is still applied but sayl-capture is not running"
        return Check(name, Status.FAIL, detail, RESTORE)
    live = gnome()
    if live is None:
        return Check(name, Status.OK, "applied" if proxy.active() else "not set by Sayl")
    mode, url = live
    if mode == "manual" and proxy.active():
        return Check(
            name,
            Status.FAIL,
            "GNOME manual mode: terminals export https_proxy, CLI tools hit the proxy",
            "restart sayld: it switches to PAC mode, which terminals ignore",
        )
    if mode == "auto" and f":{config.port}/capture/pac/" in url:
        return Check(name, Status.OK, "GNOME PAC mode (browsers only)")
    return Check(name, Status.OK, f"GNOME mode {mode}, not set by Sayl")


def _environment(config: Config, env: Mapping[str, str]) -> Check:
    marker = f"127.0.0.1:{config.capture_port}"
    hits = [name for name in PROXY_VARIABLES if marker in env.get(name, "")]
    if not hits:
        return Check("proxy variables", Status.OK, "this shell does not route through Sayl")
    return Check(
        "proxy variables",
        Status.WARN,
        f"{', '.join(hits)} point at Sayl: curl, git and API clients will be intercepted",
        "open a new terminal, or: unset " + " ".join(PROXY_VARIABLES),
    )


def _authority(directory: Path, trust: Trust) -> list[Check]:
    authority = ca.load(directory)
    if authority is None:
        return [Check("capture CA", Status.OK, "not created yet")]
    status = trust.status(authority)
    stores = [str(entry.get("store")) for entry in status["stores"]]  # type: ignore[attr-defined]
    if status["system"]:
        stores.append("system")
    if stores:
        trusted = Check("capture CA", Status.OK, f"trusted by {', '.join(sorted(set(stores)))}")
    else:
        trusted = Check("capture CA", Status.WARN, "trusted nowhere", "sayl capture install-ca")
    mode = ca.private_key_mode(directory)
    key = directory / ca.KEY_FILE
    owner = key.stat().st_uid if key.exists() else os.geteuid()
    if mode == 0o600 and owner == os.geteuid():
        private = Check("CA private key", Status.OK, "0600, owned by you")
    else:
        private = Check(
            "CA private key",
            Status.FAIL,
            f"mode {mode:o}, uid {owner}" if mode is not None else "missing",
            f"chmod 600 {key}",
        )
    return [trusted, private]


def _passthrough(config: Config, running: bool, prober: Prober) -> Check:
    name = f"{PROBE_HOST} passthrough"
    if not running:
        return Check(name, Status.OK, "skipped: capture is off")
    result = prober(config.capture_port, PROBE_HOST)
    if result.error:
        return Check(name, Status.FAIL, result.error, "add it to capture_passthrough")
    if "sayl" in result.issuer.lower():
        return Check(name, Status.FAIL, f"re-signed by {result.issuer}", "check capture_bypass")
    return Check(name, Status.OK, f"untouched, issuer {result.issuer}")


def capture_checks(
    *,
    paths: Paths,
    config: Config,
    env: Mapping[str, str] = os.environ,
    is_listening: Listening = listening,
    prober: Prober = probe,
    gnome: GnomeMode | None = None,
) -> list[Check]:
    proxy = SystemProxy(paths.data / "capture", env)
    running = is_listening(config.capture_port)
    home = Path(env.get("HOME") or Path.home())
    return [
        _listener(config, is_listening),
        *_system_proxy(config, proxy, gnome or proxy.gnome_mode, running),
        _environment(config, env),
        *_authority(ca.ca_dir(paths.data), Trust(home)),
        _passthrough(config, running, prober),
    ]
