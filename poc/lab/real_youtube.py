"""Real YouTube check through a real sayld, using little bandwidth. Needs ffprobe and internet.

Usage: uv run python poc/lab/real_youtube.py [--state DIR]
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

PORT = 6982
RATE_LIMIT = 2_000_000
VIDEO_URL = "https://www.youtube.com/watch?v=ZgZccxuj2RY"
VIDEO_FORMAT = "399+140"  # 1920x1038 AV1 plus AAC: YouTube's smallest 1080p tier for this film.
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLxgtJR7f0RBKDGzueZkVPIJySxfFFL-Ei"
PLAYLIST_ITEMS = "2,4,6,8,10"
PLAYLIST_FORMAT = "wv*+wa/w"  # Shorts have no combined stream, so pick the smallest of each.
TIMEOUT = 900.0


def call(token: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        json.dumps(body).encode() if body is not None else None,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def wait_for_token(token_file: Path) -> str:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            token = token_file.read_text()
            call(token, "/settings")
        except OSError:
            time.sleep(0.2)
        else:
            return token
    raise SystemExit("sayld did not start")


def wait_for(token: str, download_id: str) -> dict[str, Any]:
    """Follow one download, tracking its peak speed."""
    deadline = time.monotonic() + TIMEOUT
    started = time.monotonic()
    peak = 0
    while time.monotonic() < deadline:
        row: dict[str, Any] = call(token, f"/downloads/{download_id}")
        peak = max(peak, row["speed"])
        if row["status"] in ("completed", "failed"):
            return {**row, "peak_speed": peak, "seconds": round(time.monotonic() - started)}
        time.sleep(0.5)
    raise SystemExit("timed out")


def probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise SystemExit("ffprobe not found")
    found = subprocess.run(  # noqa: S603 - argument list, path from shutil.which
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    )
    data = json.loads(found.stdout)
    streams = {stream["codec_type"]: stream for stream in data["streams"]}
    return {
        "width": streams.get("video", {}).get("width"),
        "height": streams.get("video", {}).get("height"),
        "has_audio": "audio" in streams,
        "seconds": round(float(data["format"]["duration"])),
    }


def run(state: Path) -> int:
    home = state / "home"
    config = home / ".config/sayl"
    config.mkdir(parents=True, exist_ok=True)
    video_dir, playlist_dir = state / "video", state / "playlist"
    (config / "config.toml").write_text(f'port = {PORT}\ndownload_dir = "{video_dir}"\n')
    env = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
    env |= {"HOME": str(home), "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    daemon = subprocess.Popen(
        [sys.executable, "-c", "from daemon.main import main; main()"],
        env=env,
        start_new_session=True,
    )
    problems = []
    try:
        token = wait_for_token(config / "token")
        video = call(
            token,
            "/downloads",
            {"url": VIDEO_URL, "format_id": VIDEO_FORMAT, "speed_limit": RATE_LIMIT},
        )
        result = wait_for(token, video["id"])
        files = [path for path in video_dir.glob("*") if path.is_file()]
        media = probe(files[0]) if files else {}
        print(json.dumps({"video": _summary(result), "media": media}))
        if result["status"] != "completed" or not media.get("has_audio"):
            problems.append(f"video: {result['status']} {result['error']}")
        if media.get("width") != 1920 or len(files) != 1:
            problems.append(f"video files {[path.name for path in files]}, media {media}")

        playlist = call(
            token,
            "/downloads",
            {
                "url": PLAYLIST_URL,
                "format_id": PLAYLIST_FORMAT,
                "speed_limit": RATE_LIMIT,
                "playlist_items": PLAYLIST_ITEMS,
                "dest_dir": str(playlist_dir),
            },
        )
        result = wait_for(token, playlist["id"])
        names = sorted(path.name for path in playlist_dir.glob("*") if path.is_file())
        print(json.dumps({"playlist": _summary(result), "files": names}))
        if result["status"] != "completed" or len(names) != 5:
            problems.append(f"playlist: {result['status']} {len(names)} files {result['error']}")
    finally:
        os.killpg(daemon.pid, signal.SIGTERM)
        daemon.wait(timeout=30)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "engine", "filename", "seconds", "peak_speed", "done_bytes", "error")
    return {key: row[key] for key in keys}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.state is not None:
        return run(args.state.resolve())
    with tempfile.TemporaryDirectory(prefix="sayl-youtube-") as state:
        return run(Path(state))


if __name__ == "__main__":
    raise SystemExit(main())
