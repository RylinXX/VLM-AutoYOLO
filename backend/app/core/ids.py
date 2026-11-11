"""Helpers for normalizing external IDs to :class:`uuid.UUID` objects.

SQLAlchemy's ``Uuid`` column type expects ``uuid.UUID`` objects on SQLite (and
other non-native backends); passing a plain ``str`` raises
``AttributeError: 'str' object has no attribute 'hex'``.  All service/queue
code that receives IDs as strings must convert through this helper.
"""

from __future__ import annotations

import uuid

from .exceptions import NotFoundError


def to_uuid(value: str | uuid.UUID, resource: str = "Resource") -> uuid.UUID:
    """Return *value* as a UUID; invalid input maps to a 404 AppError."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise NotFoundError(resource, str(value)) from exc
