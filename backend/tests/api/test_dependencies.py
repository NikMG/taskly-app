from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from app.api.dependencies import get_repository


def test_session_creation_failure_propagates_from_repository_dependency():
    session_factory = Mock(side_effect=RuntimeError("Session unavailable"))
    app = SimpleNamespace(state=SimpleNamespace(session_factory=session_factory))
    request = Request({"type": "http", "app": app})
    dependency = get_repository(request)
    with pytest.raises(RuntimeError, match="Session unavailable"):
        next(dependency)
    session_factory.assert_called_once_with()


def _request_with_session(session):
    app = SimpleNamespace(state=SimpleNamespace(session_factory=Mock(return_value=session)))
    return Request({"type": "http", "app": app})


def test_error_in_request_rolls_back_and_closes_session():
    session = Mock()
    dependency = get_repository(_request_with_session(session))
    repo = next(dependency)
    assert repo.session is session
    with pytest.raises(RuntimeError, match="boom"):
        dependency.throw(RuntimeError("boom"))
    session.rollback.assert_called_once_with()
    session.close.assert_called_once_with()


def test_clean_request_closes_session_without_rollback():
    session = Mock()
    dependency = get_repository(_request_with_session(session))
    next(dependency)
    with pytest.raises(StopIteration):
        next(dependency)
    session.rollback.assert_not_called()
    session.close.assert_called_once_with()
