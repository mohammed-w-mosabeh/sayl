from pathlib import Path

import pytest

from daemon.capture.hints import looks_single_use
from daemon.filenames import FALLBACK, sanitize, unique_path


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../.bashrc", "bashrc"),
        ("..\\..\\evil.exe", "evil.exe"),
        ("a\x00b\n.zip", "ab.zip"),
        ("...", FALLBACK),
        ("", FALLBACK),
        ("/", FALLBACK),
    ],
)
def test_sanitize(raw: str, safe: str) -> None:
    assert sanitize(raw) == safe


def test_sanitize_caps_bytes_without_splitting_characters() -> None:
    result = sanitize("ع" * 200)
    assert len(result.encode()) <= 255
    assert result.endswith("ع")


def test_unique_path_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "a.zip").touch()
    (tmp_path / "a (1).zip").touch()
    assert unique_path(tmp_path, "a.zip").name == "a (2).zip"
    assert unique_path(tmp_path, "b.zip").name == "b.zip"


@pytest.mark.parametrize(
    ("url", "hinted"),
    [
        ("https://a.io/files/ubuntu-24.04-desktop-amd64.iso", False),
        ("https://a.io/releases/download/14.1.1/file.tar.gz", False),
        ("https://a.io/an-ordinary-long-readable-folder-name/f.zip", False),
        ("https://a.io/d/k9Xq2vLw7Rt4Zp1Bn6Ys/file.zip", True),
        ("https://a.io/f.zip?X-Amz-Signature=abc", True),
        ("https://a.io/f.zip?token=abc", True),
    ],
)
def test_single_use_hint(url: str, hinted: bool) -> None:
    assert looks_single_use(url) is hinted
