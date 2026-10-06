"""显式内置工厂：Spec 在定义时冻结，请求资源仅由 Binding 捕获。"""

from dataclasses import dataclass
from typing import Any, Callable
from backend.tools.contracts import (
    ToolBinding,
    ToolSpec,
    ToolKey,
    ToolContextPolicy,
    ToolExecutionPolicy,
    thaw,
)


@dataclass(frozen=True, slots=True)
class LocalToolFactory:
    spec: ToolSpec
    executor: Callable
    permission_subjects: Callable

    def __call__(self) -> ToolBinding:
        async def execute(context, arguments):
            return await self.executor(context, thaw(arguments))

        return ToolBinding(self.spec, execute, self.permission_subjects)


def define_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    execute: Callable,
    context_policy=None,
    execution_policy=None,
    permission_subjects=None,
) -> LocalToolFactory:
    policy = context_policy or {}
    return LocalToolFactory(
        ToolSpec(
            ToolKey("local", name),
            name,
            description,
            parameters,
            ToolContextPolicy(
                policy.get("mode", "retain"), policy.get("maxResultChars", 50_000)
            ),
            execution_policy or ToolExecutionPolicy(),
        ),
        execute,
        permission_subjects or (lambda args: (name,)),
    )
