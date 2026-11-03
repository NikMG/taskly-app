from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.lifespan import lifespan


def _app(monkeypatch, initialize):
    engine = Mock()
    factory = Mock()
    monkeypatch.setattr("app.core.lifespan.create_database_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.core.lifespan.initialize", initialize)
    monkeypatch.setattr("app.core.lifespan.create_session_factory", Mock(return_value=factory))
    app = FastAPI(lifespan=lifespan)
    app.state.database_url = "postgresql+psycopg://u:p@localhost/db"
    return app, engine, factory


def test_lifespan_sets_session_factory_and_disposes_engine(monkeypatch):
    initialize = Mock()
    app, engine, factory = _app(monkeypatch, initialize)
    with TestClient(app):
        assert app.state.session_factory is factory
        engine.dispose.assert_not_called()
    initialize.assert_called_once_with(engine)
    engine.dispose.assert_called_once_with()


def test_lifespan_disposes_engine_when_initialize_fails(monkeypatch):
    app, engine, _ = _app(monkeypatch, Mock(side_effect=RuntimeError("init failed")))
    with pytest.raises(RuntimeError, match="init failed"):
        with TestClient(app):
            pass
    engine.dispose.assert_called_once_with()
