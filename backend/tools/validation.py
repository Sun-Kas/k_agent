"""统一使用 Draft 2020-12 校验本地和 MCP 参数。"""

from typing import Any
from jsonschema import Draft202012Validator
from backend.tools.contracts import thaw


def validate_tool_arguments(schema, arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    error = next(Draft202012Validator(thaw(schema)).iter_errors(arguments), None)
    if error is not None:
        path = ".".join(str(part) for part in error.absolute_path)
        raise ValueError(f"Tool argument {path or '<root>'}: {error.message}")
