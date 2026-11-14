"""Check an unpacked .deb or AppImage: the commands are executable and no secret is inside.

python3 packaging/check_bundle.py deb <dir from dpkg-deb -x>
python3 packaging/check_bundle.py appimage <squashfs-root>
"""

import os
import sys
from pathlib import Path

RUNTIME = "usr/lib/sayl/runtime"
EXECUTABLES = {
    "deb": ["usr/bin/sayl", "usr/bin/sayld", "usr/bin/sayl-desktop", f"{RUNTIME}/bin/python3"],
    "appimage": ["usr/bin/sayl-desktop", f"{RUNTIME}/bin/python3"],
}
FILES = {"deb": ["usr/lib/systemd/user/sayld.service"], "appimage": []}
# What sayld writes at run time, and what a developer might leave lying around.
FORBIDDEN_NAMES = {"token", "headers.key", "aria2.session", ".env", "cookies.txt", "sayl.db"}
FORBIDDEN_SUFFIXES = (".session", ".key", ".p12", ".pfx")
PRIVATE_KEY = b"PRIVATE KEY-----"
SOURCE_SUFFIXES = (".py", ".pyc", ".pyi")  # Code that mentions PEM markers is not a key.


def problems(kind: str, root: Path) -> list[str]:
    found: list[str] = []
    for name in EXECUTABLES[kind]:
        path = root / name
        if not path.exists():
            found.append(f"missing: {name}")
        elif not os.access(path, os.X_OK):
            found.append(f"not executable: {name}")
    found += [f"missing: {name}" for name in FILES[kind] if not (root / name).is_file()]
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if path.name in FORBIDDEN_NAMES or path.name.endswith(FORBIDDEN_SUFFIXES):
            found.append(f"forbidden file: {relative}")
        elif (
            path.suffix not in SOURCE_SUFFIXES
            and path.stat().st_size < 64 * 1024
            and PRIVATE_KEY in path.read_bytes()
        ):
            found.append(f"private key: {relative}")
        if relative.parts[:1] == ("tests",) or "sayl-ca" in path.name:
            found.append(f"test fixture or CA: {relative}")
    return found


def main() -> int:
    kind, root = sys.argv[1], Path(sys.argv[2])
    if kind not in EXECUTABLES:
        raise SystemExit(f"unknown bundle kind {kind!r}")
    found = problems(kind, root)
    size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    print(f"{kind}: {size / 1e6:.0f} MB unpacked")
    for line in found:
        print(f"FAIL {line}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
