from typing import TypeVar

from pydantic import BaseModel

Model = TypeVar("Model", bound=BaseModel)


def validate_patch_fields(model: Model) -> Model:
    """Require a non-empty patch and allow explicit null only for the due date."""
    if not model.model_fields_set:
        raise ValueError("Provide at least one field to update.")
    for field in model.model_fields_set - {"due_date"}:
        if getattr(model, field) is None:
            raise ValueError(f"Field {field} cannot be null.")
    return model


def is_overdue(due_date, completed, today):
    """Return True when an open task has a due date before today."""
    if completed or due_date is None:
        return False
    return due_date < today
