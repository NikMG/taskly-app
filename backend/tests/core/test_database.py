from unittest.mock import Mock

from app.core.database import create_database_engine, create_session_factory, initialize


def test_engine_uses_the_postgresql_psycopg_driver():
    url = "postgresql+psycopg://taskly:taskly@localhost:5432/taskly"
    engine = create_database_engine(url)
    try:
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.url.database == "taskly"
    finally:
        engine.dispose()


def test_session_factory_is_bound_and_keeps_objects_loaded_after_commit():
    engine = create_database_engine("postgresql+psycopg://u:p@localhost/db")
    try:
        factory = create_session_factory(engine)
        session = factory()
        try:
            assert session.get_bind() is engine
            assert session.expire_on_commit is False
        finally:
            session.close()
    finally:
        engine.dispose()


def test_initialize_creates_all_tables_on_engine(monkeypatch):
    create_all = Mock()
    monkeypatch.setattr("app.core.database.Base.metadata.create_all", create_all)
    engine = Mock()
    initialize(engine)
    create_all.assert_called_once_with(engine)
