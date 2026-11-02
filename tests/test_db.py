from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from cryptography.fernet import InvalidToken
from keyring.errors import NoKeyringError
from sqlalchemy import text
from sqlmodel import Session, SQLModel

import daemon.vault
from daemon.db import alembic_config, make_engine, migrate
from daemon.models import Download, Segment
from daemon.vault import Vault, load_key


def test_migrations_match_the_models(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    migrate(path)
    with make_engine(path).connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, SQLModel.metadata) == []


def test_downgrade_removes_every_table(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    migrate(path)
    command.downgrade(alembic_config(path), "base")
    with make_engine(path).connect() as connection:
        names = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master"))}
    assert "download" not in names


def test_wal_and_foreign_keys_are_on(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    migrate(path)
    with make_engine(path).connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_a_segment_needs_its_download(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    migrate(path)
    with Session(make_engine(path)) as session:
        session.add(Download(url="http://a"))
        session.commit()
        session.add(Segment(download_id="missing", start=0, end=1))
        with pytest.raises(Exception, match="FOREIGN KEY"):
            session.commit()


def test_vault_round_trip_and_tamper() -> None:
    key = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
    vault = Vault(key)
    sealed = vault.seal({"Cookie": "a=1"})
    assert b"a=1" not in sealed
    assert vault.open(sealed) == {"Cookie": "a=1"}
    assert vault.seal({}) == b""
    assert vault.open(b"") == {}
    with pytest.raises(InvalidToken):
        vault.open(sealed[:-2] + b"xx")


def test_key_falls_back_to_a_private_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object) -> None:
        raise NoKeyringError

    monkeypatch.setattr(daemon.vault.keyring, "get_password", refuse)
    first = load_key(tmp_path / "key")
    assert load_key(tmp_path / "key") == first
    assert (tmp_path / "key").stat().st_mode & 0o777 == 0o600
