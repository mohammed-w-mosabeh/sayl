"""Sayl command line interface."""

import json
from collections.abc import Callable
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from cli.client import Client, ClientError
from cli.doctor import Status, has_failures, run_checks

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
    as_json: AsJson = False,
) -> None:
    """Add a download."""
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


@app.command()
def doctor() -> None:
    """Check aria2c, ffmpeg, the JS runtime, capture tools, and the session type."""
    checks = run_checks()
    table = Table("Check", "Status", "Detail", "Fix")
    for check in checks:
        style = _STYLES[check.status]
        table.add_row(
            check.name, f"[{style}]{check.status.value}[/{style}]", check.detail, check.fix
        )
    Console().print(table)
    if has_failures(checks):
        raise typer.Exit(code=1)
