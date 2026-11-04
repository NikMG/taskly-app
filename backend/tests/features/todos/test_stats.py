from datetime import date

from app.features.todos.schemas import TodoStats
import pytest

from app.features.todos.stats import calculate_stats, completion_percent

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


@pytest.mark.parametrize(
    ("total", "completed", "expected"),
    [(0, 0, 0), (4, 0, 0), (4, 4, 100), (4, 1, 25), (3, 1, 33), (3, 2, 66)],
)
def test_completion_percent_rounds_down_and_handles_empty(total, completed, expected):
    stats = TodoStats(
        total=total, completed=completed, active=total - completed, overdue=0
    )
    assert completion_percent(stats) == expected


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
