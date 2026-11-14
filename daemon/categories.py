"""Auto categories: pick a category from the file name or MIME type, and its folder.

Extensions and MIME prefixes are stored comma-separated, extensions without the dot. An
extension match wins over a MIME match, and the first category in name order wins a tie.
"""

import asyncio
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from daemon.filenames import unique_path
from daemon.models import Category

# Seeded once, with no folder of their own, so files keep landing where they did until the
# user gives a category a folder. The UI translates these names.
DEFAULTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "Compressed",
        "zip,rar,7z,tar,gz,tgz,bz2,tbz2,xz,txz,zst,lz,lzma,cab",
        "application/zip,application/x-7z,application/x-rar,application/vnd.rar,"
        "application/gzip,application/x-tar,application/x-xz,application/zstd",
        "archive",
    ),
    (
        "Documents",
        "pdf,doc,docx,xls,xlsx,ppt,pptx,odt,ods,odp,rtf,txt,csv,epub,mobi,djvu",
        "application/pdf,application/msword,application/vnd.openxmlformats-officedocument,"
        "application/vnd.oasis.opendocument,application/epub",
        "document",
    ),
    ("Music", "mp3,flac,wav,m4a,aac,ogg,oga,opus,wma", "audio/", "music"),
    ("Video", "mp4,mkv,webm,avi,mov,m4v,wmv,flv,mpg,mpeg,3gp", "video/", "video"),
    (
        "Programs",
        "deb,rpm,appimage,run,sh,bin,exe,msi,apk,flatpak,flatpakref,snap,jar",
        "application/x-executable,application/vnd.debian.binary-package,application/x-rpm,"
        "application/vnd.android.package-archive,application/x-msdownload",
        "program",
    ),
    (
        "Disk images",
        "iso,img,dmg,qcow2,vdi,vmdk",
        "application/x-iso9660-image,application/x-cd-image",
        "disc",
    ),
)


def defaults() -> list[Category]:
    return [
        Category(name=name, extensions=extensions, mime_prefixes=mimes, icon=icon)
        for name, extensions, mimes, icon in DEFAULTS
    ]


def _items(field: str) -> list[str]:
    return [item.strip().lower().lstrip(".") for item in field.split(",") if item.strip()]


def _extensions(filename: str) -> list[str]:
    """Longest first, so a category listing tar.gz beats one listing gz."""
    suffixes = [s.lower().lstrip(".") for s in PurePosixPath(filename).suffixes]
    return [".".join(suffixes[i:]) for i in range(len(suffixes))]


def match(categories: Iterable[Category], filename: str, mime: str = "") -> Category | None:
    ordered = sorted(categories, key=lambda c: c.name.lower())
    for extension in _extensions(filename):
        for category in ordered:
            if extension in _items(category.extensions):
                return category
    kind = mime.split(";")[0].strip().lower()
    if kind:
        for category in ordered:
            if any(kind.startswith(prefix) for prefix in _items(category.mime_prefixes)):
                return category
    return None


def folder(category: Category | None, chosen: str, default_dir: str) -> str:
    """Where a new download goes: the user's choice, else its category's folder, else default."""
    if chosen:
        return chosen
    if category is not None and category.dest_dir:
        return str(Path(category.dest_dir).expanduser())
    return default_dir


def by_id(categories: Sequence[Category], key: str | None) -> Category | None:
    return next((c for c in categories if c.id == key), None) if key else None


async def move_into(source: Path, directory: str) -> Path:
    """Move a finished file into its category's folder, without overwriting anything there."""

    def move() -> Path:
        target_dir = Path(directory).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(target_dir, source.name)
        shutil.move(source, target)
        return target

    return await asyncio.to_thread(move)
