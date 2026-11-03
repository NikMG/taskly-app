---
name: write-backend-tests
description: Use when adding or updating pytest tests under backend/tests. Gives this repository's fixtures, mocks and layout, and how to test each layer of the application with no database or network.
---

# Write backend tests

The sandbox has no Postgres, no network and no `.env`. Every test must pass there and must never open a database connection. Before writing, read `backend/tests/conftest.py`, `backend/tests/mocks.py` and the nearest existing test file, then match them.

## Conventions

- Mirror `app/` under `tests/`: `app/core/config.py` becomes `tests/core/test_config.py`. New test directories need an empty `__init__.py`, as all existing ones have. Import from `app.…`.
- Use plain functions with descriptive names that state the behaviour, such as `test_invalid_create_never_calls_repository`. No test classes.
- Use `pytest.mark.parametrize` for input variations instead of copying tests.
- Assert on outcomes: status codes, response JSON, returned objects. Assert mock calls where the call itself is the behaviour, as in `repository_mock.create.assert_called_once_with(...)` or `.assert_not_called()` on a rejected request.

## Fixtures and mocks already available

Use these; do not redefine them. Fixtures are in `conftest.py`.

- `repository_mock`: from `tests/mocks.py::make_repository_mock`, a `create_autospec(TodoRepository, instance=True)` whose `list` returns `[]`. Autospec rejects calls that do not match the real signature. Keep it that way, and never swap it for a bare `Mock()`.
- `client`: a `TestClient` over a bare `FastAPI()` with only the todos router, and `get_repository` overridden to return `repository_mock`. It does not mount health and does not run the real lifespan.
- `todo_values`: a dict of a valid task. `todo`: `Todo.model_validate(todo_values)`. `model_factory`: builds a `TodoRecord` from values.
- For `TodoRepository` itself use `Mock(spec=Session)` and pass it in, as `tests/features/todos/test_repository.py` does.
- New shared mock factories go in `tests/mocks.py`.

Never use a real engine or session for the unit under test. `create_database_engine` is safe only because SQLAlchemy connects lazily; `tests/core/test_database.py` builds one, inspects it and disposes it.

## Testing each layer

These are patterns for the kinds of code in this application. They show how to test, not what to test: that was decided by `analyze-test-impact` in pr mode or `find-coverage-gaps` in backfill mode, and this skill does not widen it. A target that fits no pattern below is still tested, by reading it and its nearest existing test. Cover the target's branches, not only what a pattern mentions.

**Route modules.** Use `client` and `repository_mock`, or a local app as under Health when `client` does not mount the router. Cover every route's outcomes: the success status and body, each `HTTPException` branch, and path or body validation. Set the mock's return value to the case under test, such as `None` for a missing record. Assert the repository was called with the parsed input, and `.assert_not_called()` when a request is rejected.

**Data access classes.** Pass in `Mock(spec=Session)`. Cover each method's branches: found and not found, what is written, and that writes are committed and a miss writes nothing. Make `session.get` return a real record from `model_factory`, then assert the changes on that record and the mapped return value. Assert `commit`, `add` and `delete` where the call is the behaviour. For fields set by the database, use a `side_effect` on `session.refresh` that fills them in.

**Config.** `resolve_database_url` calls `load_dotenv` on the repo root `.env` before reading the environment. Isolate it, or the test depends on a developer's `.env` and pollutes `os.environ` for later tests:

```python
@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr("app.core.config.load_dotenv", Mock())
    for name in ("DATABASE_URL", "POSTGRES_USER", "POSTGRES_PASSWORD",
                 "POSTGRES_HOST", "DB_PORT", "POSTGRES_DB"):
        monkeypatch.delenv(name, raising=False)
```

Behaviours worth covering: an explicit argument wins over `DATABASE_URL`, `DATABASE_URL` wins over the `POSTGRES_*` parts, the URL built from parts is correct (driver `postgresql+psycopg`, password not masked), a missing variable raises `KeyError`, and a non-numeric `DB_PORT` raises `ValueError`. Set variables only with `monkeypatch.setenv`.

**Factory.** `create_app(database_url="postgresql+psycopg://u:p@localhost/db")` needs no environment and connects to nothing until the lifespan runs. Do not enter it with `TestClient` unless the lifespan collaborators are patched. Assert `app.state.database_url` and that the expected routes are registered, using `set(app.openapi()["paths"])`. Do not read `route.path` from `app.routes`: this FastAPI version wraps included routers, so entries have no `path`.

**Lifespan.** Patch the names where they are used, in `app.core.lifespan`:

```python
engine = Mock()
monkeypatch.setattr("app.core.lifespan.create_database_engine", Mock(return_value=engine))
monkeypatch.setattr("app.core.lifespan.initialize", initialize := Mock())
monkeypatch.setattr("app.core.lifespan.create_session_factory", Mock(return_value=factory))
app = FastAPI(lifespan=lifespan)
app.state.database_url = "postgresql+psycopg://u:p@localhost/db"
with TestClient(app):
    assert app.state.session_factory is factory
engine.dispose.assert_called_once_with()
```

The important branch: when `initialize` raises, the error propagates and `engine.dispose` is still called. Use `with pytest.raises(...): with TestClient(app): ...`. `pytest-asyncio` is not installed, so do not write `async def` tests or use its markers.

**Health.** The `client` fixture does not mount this router, so build a local app. The route uses `repo.session.execute(...)`, and `session` is set in `__init__`, so autospec does not provide it. Assign it:

```python
repository_mock.session = Mock(spec=Session)
app = FastAPI()
app.include_router(health_router)
app.dependency_overrides[get_repository] = lambda: repository_mock
```

Cover the `200 {"status": "ok"}` path and the database failure path. For the failure, give `session.execute` a `side_effect` and use `TestClient(app, raise_server_exceptions=False)` to see the `500`.

**Dependency generators.** `get_repository` is a generator. Build the request as `tests/api/test_dependencies.py` does (a `SimpleNamespace` app in `Request({"type": "http", "app": app})`), then drive it by hand:

```python
dependency = get_repository(request)
next(dependency)
with pytest.raises(RuntimeError, match="boom"):
    dependency.throw(RuntimeError("boom"))
session.rollback.assert_called_once_with()
session.close.assert_called_once_with()
```

For the clean path, use `pytest.raises(StopIteration)` around a second `next(dependency)`, then assert `session.rollback.assert_not_called()` and that `close` ran once.

## Schemas and models

`schemas.py` and `models.py` are omitted from coverage, but the rules behind them are testable. Test `validators.py` and field rules through the public model, as `test_validators.py` does with `TodoUpdate`, or through the API with `client` as `test_router.py` does. Do not test that Pydantic itself works.

## The patch guard rejects these

`check_patch.py` fails the whole patch on these, so avoid them from the start:

- Files other than `test_*.py`, `conftest.py`, `__init__.py` and `tests/mocks.py`. Helpers go in `mocks.py` or `conftest.py`.
- `pytest.mark.skip`, `pytest.skip(`, `xfail`, `importorskip`, `collect_ignore`, `__test__`, and collection hooks (`pytest_collection_modifyitems`, `pytest_runtest_*`).
- `subprocess`, `os.system`, `eval(`, `exec(`, `socket`, `urllib`, `requests.`, `importlib`, `__import__(`.
- `httpx` request calls or clients. Use `TestClient`.
- `os.environ[...] = ...`, `.update(`, `.pop(` and similar writes. Use `monkeypatch`.
- Assertions on constants such as `assert True` or `assert 1 == 1`, and new test files with no assertions.
- Removing more assertions than you add.

## Before you finish a test

Ask how this test fails if the behaviour breaks. A test that passes with the production line deleted is not a test, and that includes one asserting only what a mock was told to return.

Do not assert on behaviour you believe is a bug; report it instead.

Then run it with `run-sandboxed-tests`.
