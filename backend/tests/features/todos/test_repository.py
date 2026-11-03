from unittest.mock import Mock

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.features.todos.models import TodoRecord
from app.features.todos.repository import TodoRepository
from app.features.todos.schemas import TodoCreate, TodoUpdate


def test_list_maps_models_and_orders_tasks_newest_first(model_factory, todo_values, todo):
    session = Mock(spec=Session)
    session.scalars.return_value = [model_factory(todo_values)]
    assert TodoRepository(session).list() == [todo]
    statement = session.scalars.call_args.args[0]
    expected = select(TodoRecord).order_by(TodoRecord.created_at.desc(), TodoRecord.id.desc())
    assert statement.compare(expected)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "ORDER BY todos.created_at DESC, todos.id DESC" in compiled


def test_get_returns_mapped_task(model_factory, todo_values, todo):
    session = Mock(spec=Session)
    session.get.return_value = model_factory(todo_values)
    assert TodoRepository(session).get(7) == todo


def test_create_adds_commits_and_returns_refreshed_task():
    session = Mock(spec=Session)

    def fill(record):
        record.id = 3

    session.refresh.side_effect = fill
    result = TodoRepository(session).create(TodoCreate(title="Write", priority="low"))
    record = session.add.call_args.args[0]
    assert record.title == "Write"
    assert record.created_at == record.updated_at
    session.commit.assert_called_once_with()
    session.refresh.assert_called_once_with(record)
    assert result.id == 3
    assert result.title == "Write"
    assert result.priority == "low"


def test_update_applies_only_sent_fields_and_commits(model_factory, todo_values):
    session = Mock(spec=Session)
    record = model_factory(todo_values)
    session.get.return_value = record
    result = TodoRepository(session).update(7, TodoUpdate(completed=True))
    assert record.completed is True
    assert record.title == "Plan the sprint"
    assert record.updated_at > todo_values["created_at"]
    session.commit.assert_called_once_with()
    session.refresh.assert_called_once_with(record)
    assert result.completed is True


def test_update_missing_task_returns_none_and_writes_nothing():
    session = Mock(spec=Session)
    session.get.return_value = None
    assert TodoRepository(session).update(1, TodoUpdate(completed=True)) is None
    session.commit.assert_not_called()


def test_delete_removes_and_commits(model_factory, todo_values):
    session = Mock(spec=Session)
    record = model_factory(todo_values)
    session.get.return_value = record
    assert TodoRepository(session).delete(7) is True
    session.delete.assert_called_once_with(record)
    session.commit.assert_called_once_with()


def test_delete_missing_task_returns_false_and_writes_nothing():
    session = Mock(spec=Session)
    session.get.return_value = None
    assert TodoRepository(session).delete(7) is False
    session.delete.assert_not_called()
    session.commit.assert_not_called()


def test_get_returns_none_for_missing_task():
    session = Mock(spec=Session)
    session.get.return_value = None
    assert TodoRepository(session).get(404) is None
    session.get.assert_called_once_with(TodoRecord, 404)
