"""Drive the installed Firefox through the proxy in a throwaway profile, via Marionette.

Usage: python poc/lab/firefox.py <proxy-port> <steps.json>
Steps match chromium.mjs. Certificates are accepted per session, so no CA is installed anywhere.
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MARIONETTE_PORT = 2839

PREFS = """
user_pref("marionette.port", {marionette});
user_pref("network.proxy.type", 1);
user_pref("network.proxy.http", "127.0.0.1");
user_pref("network.proxy.http_port", {proxy});
user_pref("network.proxy.ssl", "127.0.0.1");
user_pref("network.proxy.ssl_port", {proxy});
user_pref("network.proxy.allow_hijacking_localhost", true);
user_pref("network.proxy.no_proxies_on", "");
user_pref("network.http.phishy-userpass-length", 255);
user_pref("network.auth.confirmAuth.enabled", false);
user_pref("browser.download.folderList", 2);
user_pref("browser.download.dir", "{downloads}");
user_pref("browser.download.useDownloadDir", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("app.update.enabled", false);
"""


class Marionette:
    def __init__(self, port: int) -> None:
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=60)
        self.buffer = b""
        self.next_id = 0
        self._read()  # handshake

    def _read(self) -> Any:
        while b":" not in self.buffer:
            self.buffer += self.socket.recv(65536)
        size, _, rest = self.buffer.partition(b":")
        while len(rest) < int(size):
            rest += self.socket.recv(65536)
        self.buffer = rest[int(size) :]
        return json.loads(rest[: int(size)])

    def call(self, name: str, params: dict[str, Any] | None = None) -> Any:
        self.next_id += 1
        body = json.dumps([0, self.next_id, name, params or {}]).encode()
        self.socket.sendall(str(len(body)).encode() + b":" + body)
        _, _, error, result = self._read()
        if error:
            raise RuntimeError(f"{error.get('error')}: {error.get('message')}"[:200])
        return result.get("value", result) if isinstance(result, dict) else result


def run(proxy_port: str, steps_path: str) -> None:
    binary = shutil.which("firefox")
    if binary is None:
        raise SystemExit("firefox not found")
    steps = json.loads(Path(steps_path).read_text())
    with tempfile.TemporaryDirectory(prefix="sayl-ff-") as profile:
        downloads = Path(profile) / "dl"
        downloads.mkdir()
        (Path(profile) / "user.js").write_text(
            PREFS.format(marionette=MARIONETTE_PORT, proxy=proxy_port, downloads=downloads)
        )
        browser = subprocess.Popen(  # noqa: S603 - argument list, path from shutil.which
            [binary, "--headless", "--marionette", "--no-remote", "--profile", profile],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            driver = _connect()
            session = driver.call("WebDriver:NewSession", {"acceptInsecureCerts": True})
            print(json.dumps({"acceptInsecureCerts": _accepted(session)}), flush=True)
            driver.call("WebDriver:SetTimeouts", {"pageLoad": 60000})
            for step in steps:
                if step.get("quit"):
                    print(json.dumps({"step": step, "note": "browser closed"}), flush=True)
                    break
                _step(driver, step)
            print(json.dumps({"browserDownloads": sorted(p.name for p in downloads.iterdir())}))
        finally:
            browser.terminate()
            browser.wait(timeout=20)


def _accepted(session: Any) -> Any:
    return session.get("capabilities", {}).get("acceptInsecureCerts") if session else None


def _connect() -> Marionette:
    deadline = time.monotonic() + 40
    while True:
        try:
            return Marionette(MARIONETTE_PORT)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.25)


def _step(driver: Marionette, step: dict[str, Any]) -> None:
    started = time.monotonic()
    note = ""
    try:
        if "goto" in step and step.get("download"):
            # Navigate answers only once a page loads, and a captured download never loads one.
            script = "window.location.href = arguments[0];"
            driver.call("WebDriver:ExecuteScript", {"script": script, "args": [step["goto"]]})
        elif "goto" in step:
            driver.call("WebDriver:Navigate", {"url": step["goto"]})
            note = f"title={driver.call('WebDriver:GetTitle')!r}"
        elif "click" in step:
            found = driver.call(
                "WebDriver:FindElement", {"using": "css selector", "value": step["click"]}
            )
            element = next(iter(found.values())) if isinstance(found, dict) else found
            driver.call("WebDriver:ElementClick", {"id": element})
        elif "script" in step:
            driver.call("WebDriver:ExecuteScript", {"script": step["script"], "args": []})
        elif "wait" in step:
            time.sleep(step["wait"] / 1000)
    except RuntimeError as error:
        note = f"ERROR {error}"
    url = driver.call("WebDriver:GetCurrentURL").split("?")[0]
    elapsed = round((time.monotonic() - started) * 1000)
    print(json.dumps({"step": step, "ms": elapsed, "note": note, "url": url}), flush=True)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
