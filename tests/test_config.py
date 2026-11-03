import stat
from pathlib import Path

import pytest

from daemon.config import Config, Paths, ensure_token, load_config


def paths(tmp_path: Path) -> Paths:
    result = Paths.from_env({"HOME": str(tmp_path)})
    result.create()
    return result


def test_xdg_defaults_and_overrides(tmp_path: Path) -> None:
    assert paths(tmp_path).config == tmp_path / ".config/sayl"
    custom = Paths.from_env({"HOME": str(tmp_path), "XDG_DATA_HOME": "/x"})
    assert custom.data == Path("/x/sayl")


def test_token_is_private_and_stable(tmp_path: Path) -> None:
    where = paths(tmp_path)
    token = ensure_token(where)
    assert stat.S_IMODE(where.token.stat().st_mode) == 0o600
    where.token.chmod(0o644)
    assert ensure_token(where) == token
    assert stat.S_IMODE(where.token.stat().st_mode) == 0o600


def test_load_config_ignores_unknown_keys(tmp_path: Path) -> None:
    where = paths(tmp_path)
    assert load_config(where) == Config()
    (where.config / "config.toml").write_text("max_concurrent = 7\nfuture = 1\n")
    assert load_config(where).max_concurrent == 7


def test_refuses_a_non_loopback_host() -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        Config(host="0.0.0.0")  # noqa: S104 - the value under test


def test_dev_origins_are_loopback_only(tmp_path: Path) -> None:
    where = paths(tmp_path)
    (where.config / "config.toml").write_text('dev_origins = ["http://localhost:3000"]\n')
    assert load_config(where).dev_origins == ("http://localhost:3000",)
    for bad in ("https://evil.example", "http://localhost.evil.example:1", "http://[::1]:3000"):
        with pytest.raises(ValueError, match="dev_origins"):
            Config(dev_origins=(bad,))
