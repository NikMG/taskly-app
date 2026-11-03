from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import get_repository
from app.api.health import router as health_router
from tests.mocks import make_repository_mock


def _client(session):
    repository = make_repository_mock()
    repository.session = session
    app = FastAPI()
    app.include_router(health_router)
    app.dependency_overrides[get_repository] = lambda: repository
    return TestClient(app, raise_server_exceptions=False)


def test_health_returns_ok_after_querying_database():
    session = Mock(spec=Session)
    response = _client(session).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    session.execute.assert_called_once()


def test_health_returns_500_when_database_fails():
    session = Mock(spec=Session)
    session.execute.side_effect = RuntimeError("db down")
    assert _client(session).get("/api/health").status_code == 500
