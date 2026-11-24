"""The `sayld` entry point: choose the yt-dlp copy first, then import the daemon."""

import os
import sys

from daemon import ytdlp_overlay
from daemon.config import Paths


def main() -> None:
    if sys.flags.isolated:
        # A bundled sayld runs with -I. Its children (sayl-capture) run the same interpreter
        # without it, so a PYTHONHOME set by an AppImage's AppRun would break them.
        for name in ("PYTHONHOME", "PYTHONPATH"):
            os.environ.pop(name, None)
    if sys.argv[1:] == ["--restore-proxy"]:  # systemd's ExecStopPost, after any exit.
        from daemon.capture.sysproxy import main as restore

        raise SystemExit(restore(["restore"]))
    ytdlp_overlay.activate(Paths.from_env().data)
    from daemon.main import main as serve

    serve()


if __name__ == "__main__":
    main()
