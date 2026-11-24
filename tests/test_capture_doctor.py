"""`sayl doctor` capture checks and the PAC script, with no network and no desktop settings."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cli.capture_doctor import PROBE_HOST, Probe, capture_checks
from cli.doctor import Check, Status
from daemon.capture import ca, pac
from daemon.capture.sysproxy import SAVED_FILE
from daemon.config import Config, Paths

CONFIG = Config(port=6971, capture_port=6972)
PAC_URL = "http://127.0.0.1:6971/capture/pac/s3cret.pac"
HEALTHY = Probe(issuer="Google Trust Services / WE1")


def run(
    tmp_path: Path,
    *,
    running: bool = True,
    gnome: tuple[str, str] | None = ("auto", PAC_URL),
    applied: bool = True,
    env: dict[str, str] | None = None,
    probe: Probe = HEALTHY,
) -> dict[str, Check]:
    paths = Paths.from_env({"HOME": str(tmp_path)})
    if applied:
        (paths.data / "capture").mkdir(parents=True, exist_ok=True)
        (paths.data / "capture" / SAVED_FILE).write_text("{}")
    checks = capture_checks(
        paths=paths,
        config=CONFIG,
        env={"HOME": str(tmp_path), **(env or {})},
        is_listening=lambda _: running,
        prober=lambda port, host: probe,
        gnome=lambda: gnome,
    )
    return {check.name: check for check in checks}


def test_healthy_capture(tmp_path: Path) -> None:
    ca.ensure(ca.ca_dir(Paths.from_env({"HOME": str(tmp_path)}).data))
    checks = run(tmp_path)
    assert checks["capture listener"].status is Status.OK
    assert checks["system proxy"].detail == "GNOME PAC mode (browsers only)"
    assert checks["redirect rules"].status is Status.OK
    assert checks["proxy variables"].status is Status.OK
    assert checks["capture CA"].status is Status.WARN  # Created, but trusted nowhere yet.
    assert checks["CA private key"].detail == "0600, owned by you"
    passthrough = checks[f"{PROBE_HOST} passthrough"]
    assert passthrough.status is Status.OK
    assert "Google Trust Services" in passthrough.detail


def test_manual_mode_is_the_bug(tmp_path: Path) -> None:
    checks = run(tmp_path, gnome=("manual", ""))
    assert checks["system proxy"].status is Status.FAIL
    assert "terminals export https_proxy" in checks["system proxy"].detail


def test_a_leftover_setting_with_nothing_listening(tmp_path: Path) -> None:
    checks = run(tmp_path, running=False)
    assert checks["system proxy"].status is Status.FAIL
    assert checks["system proxy"].fix == "sayld --restore-proxy"
    assert checks[f"{PROBE_HOST} passthrough"].detail == "skipped: capture is off"


def test_proxy_variables_in_this_shell(tmp_path: Path) -> None:
    checks = run(tmp_path, env={"https_proxy": "http://127.0.0.1:6972/"})
    assert checks["proxy variables"].status is Status.WARN
    assert "unset" in checks["proxy variables"].fix


@pytest.mark.parametrize(
    "probe",
    [Probe(issuer="Sayl / Sayl local CA debian 1a2b3c"), Probe(error="certificate rejected")],
)
def test_passthrough_failures(tmp_path: Path, probe: Probe) -> None:
    assert run(tmp_path, probe=probe)[f"{PROBE_HOST} passthrough"].status is Status.FAIL


def test_a_loose_private_key_fails(tmp_path: Path) -> None:
    directory = ca.ca_dir(Paths.from_env({"HOME": str(tmp_path)}).data)
    ca.create(directory)
    (directory / ca.KEY_FILE).chmod(0o644)
    assert run(tmp_path)["CA private key"].status is Status.FAIL


# The PAC file, run by a real JavaScript engine with the standard helpers.
PAC_HELPERS = """
function isPlainHostName(h) { return h.indexOf(".") < 0; }
function dnsDomainIs(h, d) { return h.length >= d.length && h.slice(-d.length) === d; }
function shExpMatch(s, p) {
  var re = p.replace(/[.+^${}()|\\[\\]\\\\]/g, "\\\\$&").replace(/\\*/g, ".*").replace(/\\?/g, ".");
  return new RegExp("^" + re + "$").test(s);
}
"""


def evaluate(script: str, hosts: list[str]) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = (
        PAC_HELPERS
        + script
        + f"\nvar out = {{}}; {json.dumps(hosts)}.forEach(function (h) "
        + "{ out[h] = FindProxyForURL('https://' + h + '/', h); });"
        + "\nconsole.log(JSON.stringify(out));"
    )
    done = subprocess.run(  # noqa: S603 - argument list
        [node, "-e", program], capture_output=True, text=True, check=True, timeout=30
    )
    result: dict[str, str] = json.loads(done.stdout)
    return result


def test_pac_script_routes_only_capturable_hosts_to_the_proxy() -> None:
    script = pac.script(6972, ["anthropic.com", "claude.ai", "*.corp.test", "github.com/login"])
    proxied = "PROXY 127.0.0.1:6972; DIRECT"  # Falls back to DIRECT if the proxy is down.
    routes = evaluate(
        script,
        [
            "api.anthropic.com", "anthropic.com", "claude.ai", "downloads.claude.ai",
            "git.corp.test", "localhost", "127.0.0.1", "intranet", "app.localhost",
            "github.com", "pypi.org", "notanthropic.com", "anthropic.com.evil.test",
        ],
    )  # fmt: skip
    for host in ("api.anthropic.com", "anthropic.com", "claude.ai", "downloads.claude.ai"):
        assert routes[host] == "DIRECT", host
    for host in ("git.corp.test", "localhost", "127.0.0.1", "intranet", "app.localhost"):
        assert routes[host] == "DIRECT", host
    for host in ("github.com", "pypi.org", "notanthropic.com", "anthropic.com.evil.test"):
        assert routes[host] == proxied, host  # github.com/login needs decryption: not here.


def test_pac_secret_is_stable_and_private(tmp_path: Path) -> None:
    first = pac.secret(tmp_path / "state")
    assert pac.secret(tmp_path / "state") == first
    assert len(first) >= 32
    assert (tmp_path / "state" / pac.SECRET_FILE).stat().st_mode & 0o777 == 0o600
    assert pac.url(6971, first) == f"http://127.0.0.1:6971/capture/pac/{first}.pac"
