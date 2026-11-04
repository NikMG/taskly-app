from datetime import date

from .schemas import Todo, TodoStats


def calculate_stats(todos: list[Todo], today: date) -> TodoStats:
    completed = sum(todo.completed for todo in todos)
    overdue = sum(
        not todo.completed and todo.due_date is not None and todo.due_date < today
        for todo in todos
    )
    return TodoStats(
        total=len(todos),
        completed=completed,
        active=len(todos) - completed - overdue,
        overdue=overdue,
    )
