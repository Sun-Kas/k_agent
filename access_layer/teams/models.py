"""Public request models and stable state values for Agent Team APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AgentKind = Literal["k_agent", "codex", "claude_code"]
TeamMode = Literal["auto", "manual"]


class TeamAgentInput(BaseModel):
    """One registered Team member with an immutable runtime capability selection."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=120)
    agent_kind: AgentKind = Field(alias="agentKind")
    model_id: str | None = Field(default=None, alias="modelId")
    reasoning_effort: str | None = Field(default=None, alias="reasoningEffort")
    network_access: bool | None = Field(default=None, alias="networkAccess")
    mcp_server_ids: list[str] = Field(default_factory=list, alias="mcpServerIds")
    skill_ids: list[str] = Field(default_factory=list, alias="skillIds")
    responsibility: str = Field(default="", max_length=4000)
    is_supervisor: bool = Field(default=False, alias="isSupervisor")

    @field_validator("mcp_server_ids", "skill_ids")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        """Keep capability order while preventing duplicate runtime entries."""

        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class TeamTaskInput(BaseModel):
    """Optional manual task created together with a Team."""

    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=12_000)
    assigned_agent_index: int | None = Field(default=None, alias="assignedAgentIndex", ge=0)
    depends_on: list[int] = Field(default_factory=list, alias="dependsOn")
    priority: int = Field(default=50, ge=0, le=100)
    review_required: bool = Field(default=False, alias="reviewRequired")


class TeamCreateInput(BaseModel):
    """Create a durable Team and enough initial work to start execution."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=20_000)
    mode: TeamMode = "manual"
    workspace_dir: str | None = Field(default=None, alias="workspaceDir", max_length=4096)
    agents: list[TeamAgentInput] = Field(min_length=1, max_length=12)
    tasks: list[TeamTaskInput] = Field(default_factory=list, max_length=100)
    max_parallel: int = Field(default=4, alias="maxParallel", ge=1, le=12)

    @field_validator("agents")
    @classmethod
    def one_supervisor(cls, agents: list[TeamAgentInput]) -> list[TeamAgentInput]:
        """Require at most one explicit supervisor; the first member is fallback."""

        if sum(agent.is_supervisor for agent in agents) > 1:
            raise ValueError("Only one Team agent can be the supervisor")
        return agents

    @model_validator(mode="after")
    def validate_initial_task_references(self) -> "TeamCreateInput":
        """Reject invalid draft references before creating the Team workspace marker."""

        for index, task in enumerate(self.tasks):
            if task.assigned_agent_index is not None and task.assigned_agent_index >= len(self.agents):
                raise ValueError(f"Task {index + 1} assignedAgentIndex is out of range")
            invalid_dependencies = [
                dependency
                for dependency in task.depends_on
                if dependency < 0 or dependency >= len(self.tasks) or dependency == index
            ]
            if invalid_dependencies:
                raise ValueError(f"Task {index + 1} contains invalid dependsOn indexes")
        return self


class TeamCommandInput(BaseModel):
    """User-issued lifecycle command."""

    command: Literal["pause", "resume", "cancel"]


class TeamMessageInput(BaseModel):
    """Persist a user or Agent mailbox message."""

    model_config = ConfigDict(populate_by_name=True)

    sender_id: str = Field(default="user", alias="senderId", min_length=1)
    recipient_id: str = Field(alias="recipientId", min_length=1)
    message_type: str = Field(default="user_message", alias="messageType", min_length=1)
    content: str = Field(min_length=1, max_length=20_000)
    artifact_ids: list[str] = Field(default_factory=list, alias="artifactIds")


class TeamTaskCreateInput(BaseModel):
    """Add work to an existing Team without mutating the original plan."""

    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=12_000)
    owner_agent_id: str | None = Field(default=None, alias="ownerAgentId")
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    priority: int = Field(default=50, ge=0, le=100)
    task_type: str = Field(default="work", alias="taskType")


class TeamEventEnvelope(BaseModel):
    """Namespaced event replayed to the Team Workbench."""

    model_config = ConfigDict(populate_by_name=True)

    team_id: str = Field(alias="teamId")
    seq: int
    event_id: str = Field(alias="eventId")
    type: str
    payload: dict[str, Any]
    occurred_at: str = Field(alias="occurredAt")


SupervisorActionType = Literal[
    "approve_plan",
    "accept_submission",
    "request_revision",
    "create_task",
    "assign_task",
    "reassign_task",
    "request_review",
    "ask_human",
    "finish_team",
]


class SupervisorAction(BaseModel):
    """One validated control-plane mutation requested by the supervisor LLM."""

    model_config = ConfigDict(populate_by_name=True)

    type: SupervisorActionType
    task_id: str | None = Field(default=None, alias="taskId")
    artifact_id: str | None = Field(default=None, alias="artifactId")
    assignee_agent_id: str | None = Field(default=None, alias="assigneeAgentId")
    reviewer_agent_id: str | None = Field(default=None, alias="reviewerAgentId")
    title: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=12_000)
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    priority: int = Field(default=50, ge=0, le=100)
    reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_action_contract(self) -> "SupervisorAction":
        """Reject incomplete mutations before they reach the transactional executor."""

        task_actions = {
            "accept_submission",
            "request_revision",
            "assign_task",
            "reassign_task",
            "request_review",
        }
        if self.type in task_actions and not self.task_id:
            raise ValueError(f"{self.type} requires taskId")
        if self.type == "create_task" and not (self.title and self.description and self.assignee_agent_id):
            raise ValueError("create_task requires title, description and assigneeAgentId")
        if self.type in {"assign_task", "reassign_task"} and not self.assignee_agent_id:
            raise ValueError(f"{self.type} requires assigneeAgentId")
        if self.type == "request_review" and not self.reviewer_agent_id:
            raise ValueError("request_review requires reviewerAgentId")
        if self.type in {"request_revision", "ask_human", "finish_team"} and not self.reason.strip():
            raise ValueError(f"{self.type} requires reason")
        return self


class SupervisorDecision(BaseModel):
    """Machine-readable scheduling decision returned at a Team control boundary."""

    model_config = ConfigDict(populate_by_name=True)

    summary: str = Field(min_length=1, max_length=8000)
    actions: list[SupervisorAction] = Field(min_length=1, max_length=100)
