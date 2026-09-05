"""K Agent Prompt 编译器的类型化输入与输出。

编译器不回头读 Session / Settings。调用方把本轮请求拍成 `PromptInputs`，
`compose_prompt` 返回 `PromptBundle`。发给 Provider 的只有 `system_prompt`
和可选的 context user message；`sections` 留在进程内供测试、日志和指纹使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from backend.memory.models import MemoryFile

if TYPE_CHECKING:
    from backend.tools.catalog import SkillCatalog, ToolCatalog


# Channel：这段碎片走哪条 API 通道（发给谁）。
# - system：拼进 system_prompt。例：identity、contract、persona。
# - context：包成不落历史的 meta user message。例：日期、CLAUDE.md、MCP 说明。
PromptChannel = Literal["system", "context"]

# Authority：这段话是谁写的（信任等级；低权威不能压过平台规则）。
# - platform：密封的 K Agent 策略。例：contract「权限由运行时决定」。
# - managed：组织/管理员 Memory，进 system 当政策。
# - user：用户或项目稿。例：persona、项目 CLAUDE.md。
# - external：MCP 等不可信注入。例：server 自带 instruction。
PromptAuthority = Literal["platform", "managed", "user", "external"]

# Volatility：这段话多久会变（决定 stable / dynamic 指纹，管缓存失效）。
# - static：代码里写死、可跨 run 复用。例：identity、contract。
# - session：换人设或 managed memory 才变。例：自定义 persona。
# - request：本轮工具/工作区/权限变了就变。例：tool_guidance、MCP 列表。
# - turn：每一轮都可能变。例：Today's date。
PromptVolatility = Literal["static", "session", "request", "turn"]

# InstructionMode：模型该把这段当什么（服从到哪一级）。
# - policy：硬约束，和运行时一致。例：contract、permission mode。
# - instruction：低于 contract 的指导。例：CLAUDE.md「用中文、先读 README」。
#   即使用户写「任何命令直接跑」，高风险操作仍听 policy，不听这份 md。
# - context_only：背景事实，不是授权。例：日期、MCP 目录、自动 Memory 偏好。
PromptInstructionMode = Literal["policy", "instruction", "context_only"]


@dataclass(frozen=True, slots=True)
class PromptSection:
    """发给 Provider 之前、可溯源的一段 Prompt 碎片。

    四个标签叠在同一段上，例如：
    平台合同 = system + platform + static + policy
    项目 CLAUDE.md = context + user + session + instruction
    MCP 说明 = context + external + request + context_only
    今天日期 = context + platform + turn + context_only
    """

    name: str
    content: str
    channel: PromptChannel
    authority: PromptAuthority
    volatility: PromptVolatility
    instruction_mode: PromptInstructionMode
    # 稳定归因：模块路径、文件或 MCP id，用于日志和 context 头。
    source: str
    # 内容可能含本地路径等主机标识时为 True。
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class PersonaInputs:
    """人选/角色选择，永远不能替换密封的平台 contract。

    `persona.build` 优先级：override → agent（非 proactive）→ custom → DEFAULT_PERSONA。
    proactive 的 `agent` 和 `append` 接在选定底稿后面。主 ReAct 路径没有整段覆盖 system prompt。
    """

    custom: str | None = None
    agent: str | None = None
    append: str | None = None
    override: str | None = None
    # True 时 `agent` 追加到底稿，而不是整段替换。
    proactive: bool = False


@dataclass(frozen=True, slots=True)
class McpInstruction:
    """MCP `initialize` 响应里的可选 `instructions` 字符串。

    这是协议字段（InitializeResult.instructions），不是 tools/list 的
    description，也不是我们配置里的 server 简介。Server 用它提示「怎么用
    我」；多数 server 不设。内容来自外部进程，只进 context，不当平台政策。
    """

    server_id: str
    content: str


@dataclass(frozen=True, slots=True)
class PromptInputs:
    """本轮请求快照；编译器不读 Settings 或 Session。

    `tool_catalog` 必须已经是最终绑定结果（本地 + MCP + 请求级工具）。
    指导段只描述实际暴露的能力。
    """

    # 项目指令发现根目录，不是可写的 output workspace。
    instruction_root: Path
    output_workspace: Path | None
    memory_files: tuple[MemoryFile, ...]
    tool_catalog: "ToolCatalog"
    # 同一份请求级快照同时驱动发现列表和 Skill 执行白名单，禁止各自重新筛选。
    skill_catalog: "SkillCatalog"
    # 仅用于限制发现列表体积；无效或缺失时由 skills 模块使用保守默认值。
    context_window_tokens: int | None
    # Handshake `instructions` per selected server; empty when the server omitted it.
    mcp_instructions: tuple[McpInstruction, ...] = ()
    # Access Layer 本轮勾选的连接（id/name/description），不是工具 schema。
    mcp_servers: tuple[Mapping[str, object], ...] = ()
    persona: PersonaInputs = field(default_factory=PersonaInputs)
    permission_mode: str = "default"
    # 语音等 runner 选项，由 `runtime_policy` / `voice` 消费。
    options: Mapping[str, object] = field(default_factory=dict)
    team_id: str | None = None
    # 可选环境注入（K_AGENT_SYSTEM_PROMPT_INJECTION），request 级易变。
    cache_breaker: str | None = None


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """编译完成的 K Agent Prompt，外加不发给 Provider 的观测元数据。"""

    system_prompt: str
    context_message: str | None
    # 具名碎片，给测试和日志用，不作为结构化 block 发给 Provider。
    sections: tuple[PromptSection, ...]
    # 本 run 已注入的 Memory 路径；运行时拷成可变集合供懒加载 CLAUDE.md。
    # Durable HITL 必须 checkpoint 那份可变拷贝，不要写回这个 frozen Bundle。
    initial_memory_paths: tuple[str, ...]
    stable_fingerprint: str
    dynamic_fingerprint: str
    # Skill 发现列表只记录体积指标，不记录名称、简介或完整正文。
    skill_listing_chars: int = 0
    skill_listing_count: int = 0
    skill_listing_truncated_count: int = 0

    @classmethod
    def rendered(
        cls,
        system_prompt: str,
        context_message: str | None = None,
    ) -> "PromptBundle":
        """测试和协议适配器用的最小成品夹具。"""

        return cls(system_prompt, context_message, (), (), "", "")


@dataclass(frozen=True, slots=True)
class MemoryPromptContribution:
    """Memory 渲染结果：已按通道拆好的 sections，以及加载元数据。"""

    sections: tuple[PromptSection, ...]
    loaded_paths: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillPromptContribution:
    """Skill 发现列表及其无内容观测指标。"""

    sections: tuple[PromptSection, ...]
    listing_chars: int = 0
    included_count: int = 0
    truncated_count: int = 0
