"""Public models for local Work scheduled tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ScheduleKind = Literal["once", "daily", "weekly"]
TaskStatus = Literal["active", "paused"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "missed"]
PermissionMode = Literal["default", "full_access"]


class ScheduledTaskInput(BaseModel):
    """Validated task definition submitted by the Work UI."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    schedule_kind: ScheduleKind = Field(alias="scheduleKind")
    local_date: str | None = Field(default=None, alias="localDate")
    weekdays: list[int] = Field(default_factory=list)
    local_time: str = Field(alias="localTime", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=100)
    agent_kind: str = Field(alias="agentKind", min_length=1, max_length=80)
    model_id: str = Field(alias="modelId", min_length=1, max_length=120)
    reasoning_effort: str = Field(default="none", alias="reasoningEffort", max_length=20)
    mcp_server_ids: list[str] = Field(default_factory=list, alias="mcpServerIds")
    skill_ids: list[str] = Field(default_factory=list, alias="skillIds")
    permission_mode: PermissionMode = Field(default="default", alias="permissionMode")

    @field_validator("name", "prompt", "timezone", "agent_kind", "model_id", mode="before")
    @classmethod
    def strip_required_strings(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("weekdays must contain ISO weekday values from 1 to 7")
        return sorted(set(value))

    @field_validator("mcp_server_ids", "skill_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("capability IDs cannot be blank")
        return list(dict.fromkeys(item.strip() for item in value))

    @model_validator(mode="after")
    def validate_rule_shape(self) -> "ScheduledTaskInput":
        if self.schedule_kind == "once" and not self.local_date:
            raise ValueError("localDate is required for a once schedule")
        if self.schedule_kind != "once" and self.local_date is not None:
            raise ValueError("localDate is only valid for a once schedule")
        if self.schedule_kind == "weekly" and not self.weekdays:
            raise ValueError("weekdays is required for a weekly schedule")
        if self.schedule_kind != "weekly" and self.weekdays:
            raise ValueError("weekdays is only valid for a weekly schedule")
        return self


class ScheduledRunOutput(BaseModel):
    """One persisted trigger occurrence."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    task_id: str = Field(alias="taskId")
    trigger_type: Literal["schedule", "manual"] = Field(alias="triggerType")
    scheduled_for: str = Field(alias="scheduledFor")
    status: RunStatus
    session_id: str | None = Field(default=None, alias="sessionId")
    agent_run_id: str | None = Field(default=None, alias="agentRunId")
    started_at: str | None = Field(default=None, alias="startedAt")
    finished_at: str | None = Field(default=None, alias="finishedAt")
    error_code: str | None = Field(default=None, alias="errorCode")
    error_message: str | None = Field(default=None, alias="errorMessage")


class ScheduledApprovalResumeInput(BaseModel):
    """恢复某次 scheduled run 的 terminal Interrupt。"""

    interrupt_id: str = Field(alias="interruptId", min_length=1)
    action: Literal["approve", "deny", "cancel"]
    scope: Literal["once", "run"] = "once"


class ScheduledTaskOutput(BaseModel):
    """Task representation returned by list and detail APIs."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    prompt: str
    status: TaskStatus
    schedule_kind: ScheduleKind = Field(alias="scheduleKind")
    local_date: str | None = Field(default=None, alias="localDate")
    weekdays: list[int]
    local_time: str = Field(alias="localTime")
    timezone: str
    next_run_at: str | None = Field(default=None, alias="nextRunAt")
    agent_kind: str = Field(alias="agentKind")
    model_id: str = Field(alias="modelId")
    reasoning_effort: str = Field(alias="reasoningEffort")
    mcp_server_ids: list[str] = Field(alias="mcpServerIds")
    skill_ids: list[str] = Field(alias="skillIds")
    permission_mode: PermissionMode = Field(alias="permissionMode")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    latest_run: ScheduledRunOutput | None = Field(default=None, alias="latestRun")
