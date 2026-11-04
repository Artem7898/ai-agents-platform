"""Bounded validation feedback shared by tools and structured model results."""

import re
from typing import Any, get_args

from pydantic import ValidationError
from pydantic_core.core_schema import ErrorType

_VALIDATION_CODES = frozenset(get_args(ErrorType))


def _schema_fields(schema: dict[str, Any]) -> set[str]:
    names = set()

    def visit(value):
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return names


def safe_validation_issues(error: ValidationError, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Omit input, validator messages and untrusted keys from feedback and events."""
    fields = _schema_fields(schema)
    issues = []
    for item in error.errors(include_url=False, include_context=False, include_input=False)[:64]:
        code = item.get("type")
        code = code if code in _VALIDATION_CODES else "invalid_value"
        path = []
        length = 0
        for part in item.get("loc", ())[:8]:
            if isinstance(part, str):
                safe = (
                    part
                    if part in fields and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", part)
                    else "[field]"
                )
            elif type(part) is int and 0 <= part <= 999999:
                safe = part
            else:
                safe = "[field]"
            if length + len(str(safe)) + 1 > 128:
                break
            path.append(safe)
            length += len(str(safe)) + 1
        issues.append({"code": code, "path": path})
    return issues
