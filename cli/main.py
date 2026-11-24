"""Sayl command line interface."""

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from cli.capture_doctor import capture_checks
from cli.client import Client, ClientError
from cli.doctor import Status, has_failures, run_checks
from cli.service import Service, ServiceError, default_service, target
from daemon.batch import pattern_ranges
from daemon.config import Paths, load_config
from daemon.credentials import normalize_host

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
ID_LENGTH = 8
FINISHED = ("download.completed", "download.failed", "download.needs_refresh")
_STYLES = {Status.OK: "green", Status.WARN: "yellow", Status.FAIL: "red"}
AsJson = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]
DownloadId = Annotated[str, typer.Argument(help="A download id, or any unique start of one.")]


@app.callback()
def root() -> None:
    """Sayl download manager."""


def _client() -> Client:
    return Client()


def _fail(error: ClientError) -> typer.Exit:
    Console(stderr=True).print(f"[red]error:[/red] {escape(str(error))}")
    return typer.Exit(code=1)


def _guarded[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except ClientError as error:
        raise _fail(error) from error


def size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return ""


def progress(row: dict[str, Any]) -> str:
    total = row["total_bytes"]
    if not total:
        return size(row["done_bytes"])
    return f"{size(row['done_bytes'])} / {size(total)} ({row['done_bytes'] * 100 // total}%)"


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))


@app.command()
def add(
    url: str,
    dest: Annotated[str, typer.Option(help="Destination directory.")] = "",
    filename: Annotated[str, typer.Option(help="Save under this name.")] = "",
    format_id: Annotated[str, typer.Option("--format", help="yt-dlp format selector.")] = "",
    audio_only: Annotated[bool, typer.Option(help="Extract audio only.")] = False,
    subtitles: Annotated[str, typer.Option(help="Languages to embed, such as ar,en.")] = "",
    items: Annotated[str, typer.Option(help="Playlist items, such as 1,3,5-7.")] = "",
    limit: Annotated[int, typer.Option(help="Speed limit in bytes per second.")] = 0,
    referer: Annotated[str, typer.Option(help="Referer header.")] = "",
    paused: Annotated[bool, typer.Option(help="Add without starting.")] = False,
    checksum: Annotated[
        str, typer.Option(help="Expected checksum, as sha256:HEX or bare hex; checked at the end.")
    ] = "",
    cookies_from_browser: Annotated[
        str,
        typer.Option(help="Send the cookies this browser holds for the site: firefox, chrome, ..."),
    ] = "",
    as_json: AsJson = False,
) -> None:
    """Add a download: a link, a magnet, or a .torrent file on this machine."""
    local = Path(url).expanduser()
    if not url.startswith(("http://", "https://", "ftp://", "magnet:")) and local.is_file():
        url = local.resolve().as_uri()  # The daemon may run in another directory.
    body = {
        "url": url,
        "dest_dir": dest,
        "filename": filename,
        "format_id": format_id,
        "audio_only": audio_only,
        "subtitles": [code for code in subtitles.split(",") if code],
        "playlist_items": items,
        "speed_limit": limit,
        "referer": referer,
        "start_paused": paused,
        "checksum": checksum,
        "cookies_from_browser": cookies_from_browser,
    }
    row = _guarded(lambda: _client().call("POST", "/downloads", json=body))
    if as_json:
        _print_json(row)
    else:
        console.print(f"added {row['id'][:ID_LENGTH]}", highlight=False)


@app.command("ls")
def list_downloads(
    status: Annotated[str, typer.Option(help="Show only this status.")] = "",
    as_json: AsJson = False,
) -> None:
    """List downloads."""
    params = {"state": status} if status else {}
    rows = _guarded(lambda: _client().call("GET", "/downloads", params=params))
    if as_json:
        _print_json(rows)
        return
    table = Table("ID", "Status", "Name", "Progress", "Speed", "Engine")
    for row in rows:
        status_text = row["status"] + (" (warning)" if row["warnings"] else "")
        table.add_row(
            row["id"][:ID_LENGTH],
            status_text,
            escape(row["filename"] or row["url"]),
            progress(row),
            f"{size(row['speed'])}/s" if row["speed"] else "",
            row["engine"],
        )
    console.print(table)


def _act(download_id: str, action: str) -> dict[str, Any]:
    client = _client()
    result: dict[str, Any] = client.call(
        "POST", f"/downloads/{client.resolve(download_id)}/{action}"
    )
    return result


@app.command()
def pause(download_id: DownloadId) -> None:
    """Pause a download."""
    row = _guarded(lambda: _act(download_id, "pause"))
    console.print(f"{row['id'][:ID_LENGTH]} {row['status']}", highlight=False)


@app.command()
def resume(download_id: DownloadId) -> None:
    """Resume a paused download."""
    row = _guarded(lambda: _act(download_id, "resume"))
    console.print(f"{row['id'][:ID_LENGTH]} {row['status']}", highlight=False)


@app.command()
def refresh(download_id: DownloadId, url: str) -> None:
    """Give a download a new link. It continues from the same offset."""

    def run() -> dict[str, Any]:
        client = _client()
        path = f"/downloads/{client.resolve(download_id)}/refresh-url"
        result: dict[str, Any] = client.call("POST", path, json={"url": url})
        return result

    row = _guarded(run)
    console.print(f"{row['id'][:ID_LENGTH]} {row['status']}", highlight=False)


@app.command()
def batch(
    links: Annotated[
        list[str] | None,
        typer.Argument(help="Links, or patterns such as 'https://h/f[001-100].jpg'."),
    ] = None,
    file: Annotated[
        str, typer.Option(help="Read links from this file, one per line; - for stdin.")
    ] = "",
    dest: Annotated[str, typer.Option(help="Destination directory.")] = "",
    referer: Annotated[str, typer.Option(help="Referer header.")] = "",
    limit: Annotated[int, typer.Option(help="Speed limit per download, in bytes per second.")] = 0,
    paused: Annotated[bool, typer.Option(help="Add without starting.")] = False,
    cookies_from_browser: Annotated[
        str,
        typer.Option(help="Send the cookies this browser holds for the site: firefox, chrome, ..."),
    ] = "",
) -> None:
    """Add many downloads at once. A [1-10], [01-10], or [a-z] range expands."""
    items = list(links or [])
    if file:
        text = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text()
        items += [line.strip() for line in text.splitlines()]
    items = [item for item in items if item and not item.startswith("#")]
    patterns = [item for item in items if pattern_ranges(item)]
    body = {
        "urls": [item for item in items if not pattern_ranges(item)],
        "patterns": patterns,
        "dest_dir": dest,
        "referer": referer,
        "speed_limit": limit,
        "start_paused": paused,
        "cookies_from_browser": cookies_from_browser,
    }
    result = _guarded(lambda: _client().call("POST", "/batch", json=body))
    console.print(f"added {len(result['ids'])} downloads", highlight=False)


@app.command()
def grab(
    url: str,
    depth: Annotated[int, typer.Option(help="Links away from the start page, at most 5.")] = 2,
    ext: Annotated[
        str, typer.Option(help="Only these types, such as pdf,zip. Default: common downloads.")
    ] = "",
    min_size: Annotated[int, typer.Option(help="Smallest file in bytes.")] = 0,
    max_size: Annotated[int, typer.Option(help="Largest file in bytes, 0 = no limit.")] = 0,
    max_pages: Annotated[int, typer.Option(help="Pages to read at most.")] = 200,
    queue: Annotated[bool, typer.Option("--queue", help="Download every file found.")] = False,
    dest: Annotated[str, typer.Option(help="Destination directory, with --queue.")] = "",
    as_json: AsJson = False,
) -> None:
    """Crawl one site for files. Shows what it finds; --queue downloads them."""
    body = {
        "url": url,
        "depth": depth,
        "extensions": [item for item in ext.split(",") if item.strip()],
        "min_size": min_size,
        "max_size": max_size,
        "max_pages": max_pages,
    }

    def run() -> dict[str, Any]:
        client = _client()
        result: dict[str, Any] = client.call("POST", "/grab-site", json=body)
        while result["status"] == "running":
            time.sleep(0.5)
            result = client.call("GET", f"/grab-site/{result['id']}")
        return result

    result = _guarded(run)
    if as_json:
        _print_json(result)
    else:
        table = Table("size", "file")
        for item in result["files"]:
            table.add_row(
                size(item["size"]) if item["size"] is not None else "?", escape(item["url"])
            )
        console.print(table)
        console.print(
            f"{len(result['files'])} files on {result['pages']} pages ({result['status']})",
            highlight=False,
        )
    if result["status"] == "failed":
        raise _fail(ClientError(result["error"] or "the grab failed"))
    if queue and result["files"]:
        links = [item["url"] for item in result["files"]]
        added = _guarded(
            lambda: _client().call("POST", "/batch", json={"urls": links, "dest_dir": dest})
        )
        console.print(f"added {len(added['ids'])} downloads", highlight=False)


@app.command()
def verify(
    download_id: DownloadId,
    checksum: Annotated[
        str, typer.Argument(help="sha256:HEX, or bare hex. Default: the stored one.")
    ] = "",
) -> None:
    """Check a finished file against a checksum. Exits 1 on a mismatch."""

    def run() -> dict[str, Any]:
        client = _client()
        path = f"/downloads/{client.resolve(download_id)}/verify"
        result: dict[str, Any] = client.call("POST", path, json={"checksum": checksum})
        return result

    result = _guarded(run)
    if result["ok"]:
        console.print(f"[green]ok[/green] {result['algo']} {result['actual']}", highlight=False)
        return
    console.print(f"[red]mismatch[/red] {result['algo']}", highlight=False)
    console.print(
        f"  expected {result['expected']}\n  actual   {result['actual']}", highlight=False
    )
    raise typer.Exit(code=1)


@app.command()
def rm(download_id: DownloadId) -> None:
    """Cancel a download and forget it. Finished files stay on disk."""

    def run() -> str:
        client = _client()
        full = client.resolve(download_id)
        client.call("DELETE", f"/downloads/{full}")
        return full

    console.print(f"removed {_guarded(run)[:ID_LENGTH]}", highlight=False)


def _print_warnings(row: dict[str, Any]) -> None:
    warning = row["warnings"].get("quality_degraded")
    if warning is None:
        return
    console.print("[yellow]quality_degraded[/yellow]")
    for item in warning["items"]:
        console.print(f"  {escape(item['title'] or row['filename'])}", highlight=False)
        console.print(f"    requested: {_describe(item['requested'])}", highlight=False)
        console.print(f"    delivered: {_describe(item['delivered'])}", highlight=False)
        for reason in item["reasons"]:
            console.print(f"    {escape(reason)}", highlight=False)
    for failure in warning["solver_failures"]:
        console.print(f"  solver: {escape(failure)}", highlight=False)


def _describe(media: dict[str, Any]) -> str:
    audio = {True: "audio", False: "no audio", None: "audio not specified"}[media["has_audio"]]
    height = f"{media['height']}p" if media["height"] else "any height"
    codecs = "/".join(codec for codec in (media["vcodec"], media["acodec"]) if codec)
    return ", ".join(part for part in (height, codecs, audio) if part)


@app.command()
def info(download_id: DownloadId, as_json: AsJson = False) -> None:
    """Show one download, including any warning."""

    def run() -> dict[str, Any]:
        client = _client()
        row: dict[str, Any] = client.call("GET", f"/downloads/{client.resolve(download_id)}")
        return row

    row = _guarded(run)
    if as_json:
        _print_json(row)
        return
    fields = ("id", "url", "status", "engine", "filename", "dest_dir", "connections")
    for name in fields:
        console.print(f"{name}: {escape(str(row[name]))}", highlight=False)
    console.print(f"progress: {progress(row)}", highlight=False)
    if row["error"]:
        console.print(f"error: {escape(row['error'])}", highlight=False)
    _print_warnings(row)


def _event_line(event: dict[str, Any]) -> str:
    short = str(event.get("id", ""))[:ID_LENGTH]
    name = event["event"].removeprefix("download.")
    if name == "progress":
        total = f" / {size(event['total'])}" if event["total"] else ""
        return f"{short} {size(event['done'])}{total} {size(event['speed'])}/s"
    return f"{short} {name} {event.get('error', '')}".rstrip()


@app.command()
def watch(
    download_id: Annotated[str, typer.Argument(help="Follow only this download.")] = "",
) -> None:
    """Follow live events. With an id, stop when that download finishes."""

    def run() -> None:
        client = _client()
        target = client.resolve(download_id) if download_id else ""
        if target:
            row = client.call("GET", f"/downloads/{target}")
            if row["status"] in ("completed", "failed", "canceled", "needs_refresh"):
                console.print(f"{target[:ID_LENGTH]} {row['status']}", highlight=False)
                return
        for event in client.events():
            if target and event.get("id") != target:
                continue
            console.print(escape(_event_line(event)), highlight=False)
            if target and event["event"] in FINISHED:
                return

    try:
        _guarded(run)
    except KeyboardInterrupt:
        raise typer.Exit from None


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


@app.command()
def config(
    key: Annotated[str, typer.Argument(help="Setting name.")] = "",
    value: Annotated[str, typer.Argument(help="New value. Numbers parse as numbers.")] = "",
) -> None:
    """Show all settings, show one, or set one."""

    def run() -> dict[str, Any]:
        client = _client()
        if key and value:
            client.call("PUT", "/settings", json={key: _parse_value(value)})
        result: dict[str, Any] = client.call("GET", "/settings")
        return result

    settings = _guarded(run)
    if key and key not in settings:
        raise _fail(ClientError(f"no setting named '{key}'"))
    for name in [key] if key else sorted(settings):
        console.print(f"{name} = {json.dumps(settings[name])}", highlight=False, markup=False)


queue_app = typer.Typer(no_args_is_help=True, help="Start or stop the queue.")
app.add_typer(queue_app, name="queue")


def _print_queue(state: dict[str, Any]) -> None:
    console.print("running" if state["running"] else "stopped", highlight=False)
    if state["post_action_pending"]:
        console.print("a post action is counting down: `sayl queue cancel` stops it")
    elif state["post_action_armed"]:
        console.print("a post action runs when the queue is empty")


@queue_app.command("status")
def queue_status() -> None:
    """Show whether the queue runs, and any post action."""
    _print_queue(_guarded(lambda: _client().call("GET", "/queue")))


@queue_app.command("start")
def queue_start() -> None:
    """Start queued downloads, and resume the ones `sayl queue stop` paused."""
    _print_queue(_guarded(lambda: _client().call("POST", "/queue/start")))


@queue_app.command("stop")
def queue_stop() -> None:
    """Start nothing new and pause running downloads. Captured streams keep going."""
    _print_queue(_guarded(lambda: _client().call("POST", "/queue/stop")))


@queue_app.command("cancel")
def queue_cancel() -> None:
    """Cancel a shutdown, suspend, or command before it runs."""
    _print_queue(_guarded(lambda: _client().call("POST", "/queue/post-action/cancel")))


schedule_app = typer.Typer(no_args_is_help=True, help="Cron schedules for the queue.")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("ls")
def schedule_list(as_json: AsJson = False) -> None:
    """List schedules and when each runs next."""
    rows: list[dict[str, Any]] = _guarded(lambda: _client().call("GET", "/schedules"))
    if as_json:
        _print_json(rows)
        return
    table = Table("id", "name", "cron", "action", "after", "next run")
    for row in rows:
        action = row["action"]
        if action == "speed_limit":
            action += f" {size(row['params'].get('limit', 0))}/s"
        table.add_row(
            row["id"][:ID_LENGTH],
            escape(row["name"]),
            row["cron"],
            action,
            row["post_action"],
            row["next_run"] or "disabled",
        )
    console.print(table)


@schedule_app.command("add")
def schedule_add(
    name: str,
    cron: Annotated[str, typer.Argument(help="Five crontab fields, such as '0 2 * * *'.")],
    action: Annotated[str, typer.Argument(help="start_queue, stop_queue, or speed_limit.")],
    limit: Annotated[int, typer.Option(help="For speed_limit: bytes per second, 0 = none.")] = 0,
    after: Annotated[
        str,
        typer.Option(
            help="For start_queue, once the queue is empty: shutdown, suspend, or command."
        ),
    ] = "none",
    command: Annotated[
        str, typer.Option(help="For --after command: the program and arguments.")
    ] = "",
) -> None:
    """Add a schedule."""
    params: dict[str, Any] = {}
    if action == "speed_limit":
        params["limit"] = limit
    if command:
        params["command"] = command
    body = {"name": name, "cron": cron, "action": action, "params": params, "post_action": after}
    row = _guarded(lambda: _client().call("POST", "/schedules", json=body))
    console.print(f"added {row['id'][:ID_LENGTH]}, next run {row['next_run']}", highlight=False)


def _schedule_id(client: Client, prefix: str) -> str:
    matches = [r["id"] for r in client.call("GET", "/schedules") if r["id"].startswith(prefix)]
    if len(matches) != 1:
        raise ClientError(f"{len(matches)} schedules start with {prefix!r}")
    return str(matches[0])


@schedule_app.command("rm")
def schedule_remove(schedule_id: str) -> None:
    """Delete a schedule."""

    def run() -> str:
        client = _client()
        full = _schedule_id(client, schedule_id)
        client.call("DELETE", f"/schedules/{full}")
        return full

    console.print(f"removed {_guarded(run)[:ID_LENGTH]}", highlight=False)


@schedule_app.command("run")
def schedule_run(schedule_id: str) -> None:
    """Do what a schedule does, now."""

    def run() -> str:
        client = _client()
        full = _schedule_id(client, schedule_id)
        client.call("POST", f"/schedules/{full}/run")
        return full

    console.print(f"ran {_guarded(run)[:ID_LENGTH]}", highlight=False)


creds_app = typer.Typer(no_args_is_help=True, help="Logins for sites that need HTTP auth.")
app.add_typer(creds_app, name="creds")


@creds_app.command("ls")
def creds_list(as_json: AsJson = False) -> None:
    """List the hosts with a stored login. Passwords are never shown."""
    rows: list[dict[str, Any]] = _guarded(lambda: _client().call("GET", "/credentials"))
    if as_json:
        _print_json(rows)
        return
    table = Table("host", "username")
    for row in rows:
        table.add_row(escape(row["host"]), escape(row["username"]))
    console.print(table)


@creds_app.command("set")
def creds_set(
    host: Annotated[str, typer.Argument(help="A host name, or a URL on that host.")],
    username: str,
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help="Read the password from standard input.")
    ] = False,
) -> None:
    """Store the login for a host. The password is asked for, never taken as an argument."""
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = typer.prompt("Password", hide_input=True)
    body = {"username": username, "password": password}
    path = f"/credentials/{quote(normalize_host(host) or host, safe='')}"
    row = _guarded(lambda: _client().call("PUT", path, json=body))
    console.print(f"saved the login for {escape(row['host'])}", highlight=False)


@creds_app.command("rm")
def creds_remove(host: str) -> None:
    """Delete the login for a host."""
    path = f"/credentials/{quote(normalize_host(host) or host, safe='')}"
    _guarded(lambda: _client().call("DELETE", path))
    console.print(f"removed the login for {escape(host)}", highlight=False)


history_app = typer.Typer(no_args_is_help=True, help="Export or import the download history.")
app.add_typer(history_app, name="history")


@history_app.command("export")
def history_export(
    file: Annotated[Path | None, typer.Argument(help="Write here instead of stdout.")] = None,
) -> None:
    """Export every download, without headers, cookies, or logins."""
    document = _guarded(lambda: _client().call("GET", "/history/export"))
    text = json.dumps(document, indent=1, ensure_ascii=False)
    if file is None:
        print(text)
        return
    file.write_text(text + "\n")
    console.print(f"exported {len(document['downloads'])} downloads", highlight=False)


@history_app.command("import")
def history_import(file: Path) -> None:
    """Add the downloads of an export. Nothing starts; known ids are skipped."""
    try:
        document = json.loads(file.read_text())
    except (OSError, ValueError) as error:
        raise _fail(ClientError(f"cannot read {file}: {error}")) from error
    result = _guarded(lambda: _client().call("POST", "/history/import", json=document))
    console.print(f"added {result['added']}, skipped {result['skipped']}", highlight=False)


@app.command()
def stats(as_json: AsJson = False) -> None:
    """Totals, the last 30 days, and the busiest hosts."""
    body: dict[str, Any] = _guarded(lambda: _client().call("GET", "/stats"))
    if as_json:
        _print_json(body)
        return
    counts = ", ".join(f"{name} {count}" for name, count in body["by_status"].items())
    console.print(f"downloads: {counts or 'none'}", highlight=False)
    console.print(
        f"completed: {body['completed']} ({size(body['completed_bytes'])}), "
        f"average {size(body['average_speed'])}/s",
        highlight=False,
    )
    month = sum(day["bytes"] for day in body["per_day"])
    console.print(f"last 30 days: {size(month)}", highlight=False)
    if body["top_hosts"]:
        table = Table("host", "downloaded")
        for row in body["top_hosts"]:
            table.add_row(escape(row["host"]), size(row["bytes"]))
        console.print(table)


capture_app = typer.Typer(
    no_args_is_help=True, help="Capture browser downloads through the local proxy."
)
app.add_typer(capture_app, name="capture")
PROMPT_TIMEOUT = 180  # pkexec waits for a person to type a password.


def _print_capture(status: dict[str, Any]) -> None:
    style = {"running": "green", "failed": "red"}.get(status["state"], "yellow")
    console.print(f"state         [{style}]{status['state']}[/{style}]")
    console.print(f"proxy         127.0.0.1:{status['port']}", highlight=False)
    console.print(f"system proxy  {'on' if status['system_proxy'] else 'off'}")
    authority = status["ca"]
    if authority is None:
        console.print("CA            not created yet")
    else:
        stores = ", ".join(sorted({item["store"] for item in authority["stores"]})) or "none"
        console.print(f"CA            {escape(authority['name'])}", highlight=False)
        console.print(f"fingerprint   {authority['fingerprint']}", highlight=False)
        console.print(f"trusted by    {stores}{', system' if authority['system'] else ''}")
    if status["error"]:
        console.print(f"[yellow]{escape(status['error'])}[/yellow]")


def _print_report(report: dict[str, Any]) -> None:
    table = Table("Store", "Target", "Result", "Detail")
    for step in report["steps"]:
        outcome = step["outcome"]
        style = "red" if outcome == "failed" else "green"
        table.add_row(
            step["store"], step["target"], f"[{style}]{outcome}[/{style}]", escape(step["detail"])
        )
    console.print(table)
    if not report["ok"]:
        raise typer.Exit(code=1)


@capture_app.command("status")
def capture_status(as_json: AsJson = False) -> None:
    """Show whether capture runs, the system proxy, and where the CA is trusted."""
    status: dict[str, Any] = _guarded(lambda: _client().call("GET", "/capture"))
    if as_json:
        _print_json(status)
        return
    _print_capture(status)


@capture_app.command("enable")
def capture_enable(
    system_proxy: Annotated[
        bool, typer.Option(help="Point the desktop's proxy setting at Sayl.")
    ] = True,
) -> None:
    """Start the capture proxy. The previous proxy settings are saved first."""
    status: dict[str, Any] = _guarded(
        lambda: _client().call(
            "PUT", "/capture", json={"enabled": True, "system_proxy": system_proxy}
        )
    )
    _print_capture(status)
    if status["state"] != "running":
        raise typer.Exit(code=1)
    if status["ca"] and not status["ca"]["stores"] and not status["ca"]["system"]:
        console.print(
            "[yellow]No browser trusts the Sayl CA yet. Run: sayl capture install-ca[/yellow]"
        )


@capture_app.command("disable")
def capture_disable() -> None:
    """Stop the capture proxy and restore the previous proxy settings."""
    _print_capture(_guarded(lambda: _client().call("PUT", "/capture", json={"enabled": False})))


@capture_app.command("install-ca")
def capture_install_ca(
    system: Annotated[
        bool, typer.Option(help="Also add it to the system store (asks for a password).")
    ] = True,
) -> None:
    """Create this machine's CA if needed, and trust it in Chrome, Firefox, and the system."""
    report: dict[str, Any] = _guarded(
        lambda: _client().call(
            "POST", "/capture/ca", json={"system": system}, timeout=PROMPT_TIMEOUT
        )
    )
    _print_report(report)


@capture_app.command("uninstall")
def capture_uninstall() -> None:
    """Restore the proxy settings, stop capturing, and remove the CA from every store."""
    report: dict[str, Any] = _guarded(
        lambda: _client().call("POST", "/capture/uninstall", timeout=PROMPT_TIMEOUT)
    )
    _print_report(report)


@app.command()
def doctor() -> None:
    """Check tools, the session, and capture: listener, system proxy, CA, passthrough."""
    paths = Paths.from_env()
    checks = [*run_checks(), *capture_checks(paths=paths, config=load_config(paths))]
    table = Table("Check", "Status", "Detail", "Fix")
    for check in checks:
        style = _STYLES[check.status]
        table.add_row(
            check.name, f"[{style}]{check.status.value}[/{style}]", check.detail, check.fix
        )
    Console().print(table)
    if has_failures(checks):
        raise typer.Exit(code=1)


service_app = typer.Typer(
    no_args_is_help=True, help="Run sayld as a systemd user service, started at login."
)
app.add_typer(service_app, name="service")


def _service() -> Service:
    return default_service(load_config(Paths.from_env()).port)


def _service_guarded[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except ServiceError as error:
        console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(1) from error


@service_app.command("install")
def service_install(
    start: Annotated[bool, typer.Option(help="Start it now as well as at login.")] = True,
) -> None:
    """Enable sayld at login and start it. Safe to run again; it repairs a stale unit."""
    for step in _service_guarded(lambda: _service().install(start=start)):
        console.print(step, highlight=False)


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop sayld, disable it at login, and remove the unit Sayl wrote."""
    for step in _service_guarded(lambda: _service().uninstall()):
        console.print(step, highlight=False)


@service_app.command("status")
def service_status() -> None:
    """Show whether the unit is enabled and running, and which sayld it starts."""
    service = _service()
    enabled, active = _service_guarded(lambda: (service.enabled(), service.active()))
    console.print(f"enabled  {'yes' if enabled else 'no'}")
    console.print(f"running  {'yes' if active else 'no'}")
    console.print(f"starts   {' '.join(target(service.env).command)}", highlight=False)


@service_app.command("restart")
def service_restart() -> None:
    """Restart sayld, for example to use a yt-dlp the updater staged."""
    _service_guarded(lambda: _service().restart())
    console.print("restarted")


ytdlp_app = typer.Typer(no_args_is_help=True, help="The yt-dlp sayld uses, and its updates.")
app.add_typer(ytdlp_app, name="ytdlp")


def _print_ytdlp(state: dict[str, Any]) -> None:
    console.print(f"active   {state['active']}", highlight=False)
    console.print(f"bundled  {state['bundled']}", highlight=False)
    if state["restart_pending"]:
        console.print(
            f"staged   {state['staged']}: run `sayl service restart` to use it", highlight=False
        )
    if state["error"]:
        console.print(f"[red]{escape(state['error'])}[/red]")


@ytdlp_app.command("status")
def ytdlp_status() -> None:
    """Show the active, bundled, and staged yt-dlp versions."""
    info: dict[str, Any] = _guarded(lambda: _client().call("GET", "/info"))
    if not info.get("ytdlp"):
        raise _fail(ClientError("this sayld does not report yt-dlp"))
    _print_ytdlp(info["ytdlp"])


@ytdlp_app.command("update")
def ytdlp_update() -> None:
    """Check PyPI now; a newer yt-dlp is verified and used from sayld's next start."""
    state: dict[str, Any] = _guarded(lambda: _client().call("POST", "/ytdlp/update"))
    if not state["restart_pending"] and not state["error"]:
        console.print(f"yt-dlp {state['active']} is the latest", highlight=False)
        return
    _print_ytdlp(state)


if __name__ == "__main__":
    app(prog_name="sayl")
