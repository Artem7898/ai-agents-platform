"""Pure JSON Schema normalization shared by provider requests and contracts."""

import copy
from typing import Any


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize Pydantic's object schemas to the provider's strict subset.

    Open-ended mappings cannot be represented in that subset and fail before a
    provider request. Defaults stay in the domain model, not the wire schema.
    """
    result = copy.deepcopy(schema)

    def visit(value: dict[str, Any]) -> None:
        # Schema containers such as `properties` are maps of user-chosen names;
        # process their values rather than treating the container as a schema.
        value.pop("default", None)
        if "patternProperties" in value:
            raise ValueError("Strict output schemas cannot contain patterned mappings")
        if value.get("type") == "object" or "properties" in value:
            extra = value.get("additionalProperties", False)
            if extra is not False:
                raise ValueError("Strict output schemas cannot contain open-ended mappings")
            properties = value.setdefault("properties", {})
            value["required"] = list(properties)
            value["additionalProperties"] = False
        for key in ("properties", "$defs", "definitions"):
            for child in value.get(key, {}).values():
                if isinstance(child, dict):
                    visit(child)
        for key in ("items", "not", "contains"):
            child = value.get(key)
            if isinstance(child, dict):
                visit(child)
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in value.get(key, []):
                if isinstance(child, dict):
                    visit(child)

    visit(result)
    return result
