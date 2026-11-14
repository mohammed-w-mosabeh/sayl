"""Build the Python runtime the .deb and the AppImage carry in /usr/lib/sayl/runtime.

A copy of a python-build-standalone CPython 3.12 (as `uv python install` gets it), with sayl
and its locked dependencies, including the `capture` group, installed into its own
site-packages. The tree is relocatable: launchers run `runtime/bin/python3 -I -m ...`.

Usage: uv run python packaging/build_runtime.py [--out build/runtime]
"""

import argparse
import compileall
import py_compile
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = "3.12"
# Never needed at run time; Tk would also pull in X11 libraries the bundle does not declare.
STRIP_STDLIB = ("test", "idlelib", "tkinter", "turtledemo", "turtle.py", "ensurepip", "lib2to3")
STRIP_PACKAGES = ("pip", "setuptools", "wheel")


def run(*command: str | Path, cwd: Path = ROOT) -> str:
    print("+", " ".join(str(part) for part in command), flush=True)
    done = subprocess.run(  # noqa: S603 - argument list
        [str(part) for part in command], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout


def standalone_python() -> Path:
    """The prefix of a uv-managed (python-build-standalone) CPython 3.12."""
    run("uv", "python", "install", PYTHON)
    found = run("uv", "python", "find", "--managed-python", PYTHON).strip()
    prefix = Path(found).resolve().parent.parent
    if not (prefix / "lib" / f"python{PYTHON}").is_dir():
        raise SystemExit(f"unexpected layout under {prefix}")
    return prefix


def stdlib(runtime: Path) -> Path:
    return runtime / "lib" / f"python{PYTHON}"


def install(runtime: Path, work: Path) -> None:
    python = runtime / "bin" / "python3"
    (stdlib(runtime) / "EXTERNALLY-MANAGED").unlink(missing_ok=True)
    requirements = work / "requirements.txt"
    run(
        "uv", "export", "--frozen", "--no-dev", "--group", "capture", "--no-emit-project",
        "--format", "requirements-txt", "--output-file", requirements,
    )  # fmt: skip
    run("uv", "pip", "install", "--python", python, "--require-hashes", "-r", requirements)
    run("uv", "build", "--wheel", "--out-dir", work)
    wheel = next(work.glob("sayl-*.whl"))
    run("uv", "pip", "install", "--python", python, "--no-deps", wheel)


def strip(runtime: Path) -> None:
    for name in STRIP_STDLIB:
        target = stdlib(runtime) / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    site = stdlib(runtime) / "site-packages"
    for name in STRIP_PACKAGES:
        for entry in site.glob(f"{name}*"):
            if entry.name == name or entry.name.startswith(f"{name}-"):
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    # Dependencies' own test suites: never imported, and tornado's holds a private key.
    for pattern in ("*/test", "*/tests", "*/*/test", "*/*/tests"):
        for entry in list(site.glob(pattern)):
            if entry.is_dir():
                shutil.rmtree(entry)
    for entry in (runtime / "include", runtime / "share"):
        shutil.rmtree(entry, ignore_errors=True)
    for entry in (stdlib(runtime) / "lib-dynload").glob("_tkinter*"):
        entry.unlink()  # Links against the Tcl/Tk libraries removed below.
    for pattern in ("libtcl*", "libtk*", "tcl*", "tk*", "itcl*", "thread*", "pkgconfig"):
        for entry in (runtime / "lib").glob(pattern):
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    # Console scripts carry the build machine's interpreter path in their shebang.
    for entry in (runtime / "bin").iterdir():
        if not entry.name.startswith("python") or entry.name.endswith("-config"):
            entry.unlink()
    for cache in list(runtime.rglob("__pycache__")):
        shutil.rmtree(cache)


def precompile(runtime: Path) -> None:
    """/usr is read-only, so write the bytecode now. Hash-checked pycs ignore mtimes."""
    ok = compileall.compile_dir(
        stdlib(runtime),
        quiet=1,
        workers=0,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        stripdir=str(runtime),
        prependdir="/usr/lib/sayl/runtime",
    )
    if not ok:
        print("warning: some files did not compile (often test data inside packages)")


def unresolved(runtime: Path) -> list[str]:
    """Shared objects whose libraries the dynamic linker cannot find (linuxdeploy refuses them)."""
    missing: list[str] = []
    for path in runtime.rglob("*.so*"):
        if path.is_symlink() or not path.is_file():
            continue
        done = subprocess.run(  # noqa: S603 - argument list
            ["ldd", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        missing += [
            f"{path}: {line.strip()}" for line in done.stdout.splitlines() if "not found" in line
        ]
    return missing


def smoke(runtime: Path) -> None:
    if missing := unresolved(runtime):
        raise SystemExit("unresolved libraries:\n" + "\n".join(missing))
    python = runtime / "bin" / "python3"
    if "service" not in run(python, "-I", "-m", "cli.main", "--help"):
        raise SystemExit("the bundled sayl CLI printed no help")
    run(
        python, "-I", "-c",
        "import daemon.launch, daemon.main, daemon.capture.run, yt_dlp, yt_dlp_ejs, mitmproxy",
    )  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "runtime")
    args = parser.parse_args()
    if sysconfig.get_platform() != "linux-x86_64":
        print(f"warning: building on {sysconfig.get_platform()}", file=sys.stderr)
    out: Path = args.out.resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(standalone_python(), out, symlinks=True)
    with tempfile.TemporaryDirectory() as work:
        install(out, Path(work))
    strip(out)
    precompile(out)
    # Prove the tree is relocatable before it is packaged.
    with tempfile.TemporaryDirectory() as moved:
        relocated = Path(moved) / "runtime"
        shutil.copytree(out, relocated, symlinks=True)
        smoke(relocated)
    size = sum(path.stat().st_size for path in out.rglob("*") if path.is_file())
    print(f"runtime ready in {out} ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
