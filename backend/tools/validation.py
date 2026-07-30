"""Validate tool arguments against the supported JSON Schema subset."""

from __future__ import annotations

from typing import Any


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Raise ``ValueError`` when arguments violate the supported schema subset."""

    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for name in required:
        if name not in arguments:
            raise ValueError(f'Missing required tool argument: "{name}"')

    allow_additional = schema.get("additionalProperties", True)
    if not allow_additional:
        unknown = set(arguments) - set(properties)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"Unexpected tool arguments: {names}")

    for key, value in arguments.items():
        property_schema = properties.get(key)
        if property_schema is None:
            continue
        _validate_value(key, value, property_schema)


def _validate_value(name: str, value: Any, schema: dict[str, Any]) -> None:
    """按 JSON schema 递归校验单个参数值。"""
    schema_type = schema.get("type")
    if schema_type == "string" and not isinstance(value, str):
        raise ValueError(f'Tool argument "{name}" must be a string.')
    if schema_type == "number" and not isinstance(value, (int, float)):
        raise ValueError(f'Tool argument "{name}" must be a number.')
    if schema_type == "integer" and not isinstance(value, int):
        raise ValueError(f'Tool argument "{name}" must be an integer.')
    if schema_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f'Tool argument "{name}" must be a boolean.')
    if schema_type == "array" and not isinstance(value, list):
        raise ValueError(f'Tool argument "{name}" must be an array.')
    if schema_type == "object" and not isinstance(value, dict):
        raise ValueError(f'Tool argument "{name}" must be an object.')
