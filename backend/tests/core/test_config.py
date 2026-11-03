from unittest.mock import Mock

import pytest

from app.core.config import resolve_database_url

PARTS = {
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "s3cret",
    "POSTGRES_HOST": "dbhost",
    "DB_PORT": "5433",
    "POSTGRES_DB": "taskly",
}


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr("app.core.config.load_dotenv", Mock())
    for name in ("DATABASE_URL", *PARTS):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _set_parts(monkeypatch):
    for name, value in PARTS.items():
        monkeypatch.setenv(name, value)


def test_explicit_argument_wins_over_environment(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+psycopg://env/db")
    assert resolve_database_url("postgresql+psycopg://arg/db") == "postgresql+psycopg://arg/db"


def test_database_url_wins_over_parts(clean_env):
    _set_parts(clean_env)
    clean_env.setenv("DATABASE_URL", "postgresql+psycopg://env/db")
    assert resolve_database_url() == "postgresql+psycopg://env/db"


def test_url_built_from_parts_keeps_password(clean_env):
    _set_parts(clean_env)
    assert resolve_database_url() == "postgresql+psycopg://user:s3cret@dbhost:5433/taskly"


def test_missing_part_raises_key_error(clean_env):
    _set_parts(clean_env)
    clean_env.delenv("POSTGRES_HOST")
    with pytest.raises(KeyError):
        resolve_database_url()


def test_non_numeric_port_raises_value_error(clean_env):
    _set_parts(clean_env)
    clean_env.setenv("DB_PORT", "abc")
    with pytest.raises(ValueError):
        resolve_database_url()
