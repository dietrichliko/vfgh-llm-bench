from pathlib import Path

import pytest

from bench.settings import Settings


def test_secret_from_secrets_dir(tmp_path: Path) -> None:
    (tmp_path / "API_KEY").write_text("from-file\n")
    assert Settings(secrets_dir=tmp_path).secret("API_KEY") == "from-file"


def test_secret_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "from-env")
    assert Settings(secrets_dir=tmp_path).secret("API_KEY") == "from-env"


def test_secrets_dir_wins_over_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "API_KEY").write_text("from-file")
    monkeypatch.setenv("API_KEY", "from-env")
    assert Settings(secrets_dir=tmp_path).secret("API_KEY") == "from-file"


def test_missing_or_empty_secret_is_none(tmp_path: Path) -> None:
    (tmp_path / "EMPTY").write_text("  \n")
    s = Settings(secrets_dir=tmp_path)
    assert s.secret("MISSING") is None
    assert s.secret("EMPTY") is None
    assert not s.has_secret("EMPTY")


def test_admin_emails_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_ADMIN_EMAILS", " A@Example.org, b@example.org ,")
    assert Settings().admin_emails == ["a@example.org", "b@example.org"]


def test_default_database_is_sqlite_in_data_dir(tmp_path: Path) -> None:
    assert Settings(data_dir=tmp_path).db_url == f"sqlite:///{tmp_path}/bench.sqlite"
