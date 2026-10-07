"""Provider 兼容输入与错误投影，不读取工具结果控制权限。"""

import json
from typing import Any


def _decode_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    """Decode provider arguments while enforcing the object tool contract."""

    # 无参工具的参数串通常为空，视为空对象而不是解析错误。
    if not raw_arguments:
        return {}
    decoded = json.loads(raw_arguments)
    # 顶层必须是对象：后续按 key 取参数，数组或标量会在更深处引发
    # 难以定位的类型错误。
    if not isinstance(decoded, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    return decoded


def _skill_alias_arguments(
    skill_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Convert a provider's direct Skill call to the canonical Skill schema."""

    raw_args = arguments.get("args")
    supplied_skill = arguments.get("skill")
    if raw_args is None and supplied_skill not in (None, "", skill_name):
        # Some compatible providers change the function name to the Skill
        # name, then put the user's topic into the old `skill` field.
        raw_args = supplied_skill
    if raw_args is None:
        # 兜底：取第一个非空的其他字段当作参数。别名调用下字段名不可预期，
        # 拿不到就退回空串，交给 Skill 工具自己报缺参，而不是在这里抛异常。
        raw_args = next(
            (
                value
                for key, value in arguments.items()
                if key != "skill" and value not in (None, "")
            ),
            "",
        )
    if not isinstance(raw_args, str):
        raw_args = json.dumps(raw_args, ensure_ascii=False)
    return {"skill": skill_name, "args": raw_args}


def _recoverable_tool_error(*, tool_name: str, error: Exception) -> str:
    """Convert an already-observed ordinary tool failure for model recovery."""

    message = str(error).strip() or "Tool execution failed without an error message."
    return json.dumps(
        {
            "ok": False,
            "tool": tool_name,
            "error": message,
            "errorType": type(error).__name__,
        },
        ensure_ascii=False,
    )
