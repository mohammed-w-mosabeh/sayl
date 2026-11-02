"""Sayl command line interface."""

import typer
from rich.console import Console
from rich.table import Table

from cli.doctor import Status, has_failures, run_checks

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def root() -> None:
    """Sayl download manager."""


_STYLES = {Status.OK: "green", Status.WARN: "yellow", Status.FAIL: "red"}


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
