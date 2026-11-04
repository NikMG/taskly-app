from datetime import date

from app.features.todos.schemas import TodoStats
from app.features.todos.stats import calculate_stats

TODAY = date(2026, 10, 15)


def make(todo, **changes):
    return todo.model_copy(update=changes)


def test_empty_list_gives_zero_counts():
    assert calculate_stats([], TODAY) == TodoStats(total=0, completed=0, active=0, overdue=0)


def test_counts_completed_and_active(todo):
    todos = [make(todo, completed=True), make(todo, completed=False), make(todo, completed=False)]
    assert calculate_stats(todos, TODAY) == TodoStats(
        total=3, completed=1, active=2, overdue=0
    )


def test_overdue_only_counts_active_tasks_due_before_today(todo):
    todos = [
        make(todo, due_date=date(2026, 10, 14)),  # overdue
        make(todo, due_date=TODAY),  # due today is not overdue
        make(todo, due_date=date(2026, 10, 16)),  # future
        make(todo, due_date=None),  # no due date
        make(todo, due_date=date(2026, 10, 1), completed=True),  # completed
    ]
    assert calculate_stats(todos, TODAY) == TodoStats(
        total=5, completed=1, active=4, overdue=1
    )
