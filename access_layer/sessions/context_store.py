"""K Agent 上下文派生状态：校验、CAS、失效与公开只读投影。

完整对话仍以 ``history.jsonl`` 为真相源。本模块只保存可删除重建的 Provider
活动视图；所有提交都以 revision CAS，不能依赖单进程 ``asyncio.Lock``。
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any

from access_layer.schemas import ChatMessage
from access_layer.sessions.history import (
    message_seq_index,
    messages_from_records,
    projected_prefix_digest,
)
from access_layer.settings import get_or_init_settings
from access_layer.logging_config import log_event
from access_layer.storage import StorageBackend


class ContextStateConflict(RuntimeError):
    """提案基于过期 generation/revision，不能覆盖较新的派生状态。"""


class ContextStateInvalid(RuntimeError):
    """提案 boundary 不存在或会拆开不完整历史语义组。"""


def empty_context_state(session_id: str) -> dict[str, Any]:
    """返回尚未 compact 的完整 v1 状态，便于失败熔断也能独立持久化。"""

    return {
        "schemaVersion": 1,
        "sessionId": session_id,
        "generation": 0,
        "revision": 0,
        "boundary": None,
        "summary": None,
        "toolReplacements": [],
        "workingSet": {"recentFiles": [], "invokedSkillIds": [], "plan": None},
        "failureState": {
            "consecutiveAutoFailures": 0,
            "autoDisabled": False,
            "lastFailureCode": None,
        },
        "pendingContinuation": None,
        "lastProposalId": None,
        "stats": {},
    }


def normalize_context_state(session_id: str, raw: Any) -> dict[str, Any]:
    """兼容升级旧的 seq/summary 简化格式，始终返回完整状态结构。"""

    base = empty_context_state(session_id)
    if not isinstance(raw, dict):
        return base
    if isinstance(raw.get("boundary"), dict) or "generation" in raw:
        result = {**base, **copy.deepcopy(raw), "sessionId": session_id}
        result["failureState"] = {
            **base["failureState"],
            **(result.get("failureState") if isinstance(result.get("failureState"), dict) else {}),
        }
        result["workingSet"] = {
            **base["workingSet"],
            **(result.get("workingSet") if isinstance(result.get("workingSet"), dict) else {}),
        }
        result["toolReplacements"] = (
            result.get("toolReplacements")
            if isinstance(result.get("toolReplacements"), list)
            else []
        )
        return result

    # 2026-09-03 的基础协议把 boundary 字段平铺在顶层；保留已有摘要并升为 generation 1。
    covered_seq = raw.get("coveredThroughSeq")
    covered_id = raw.get("coveredThroughMessageId")
    digest = raw.get("coveredPrefixDigest")
    summary_text = raw.get("summary")
    if isinstance(covered_seq, int) and isinstance(covered_id, str) and isinstance(digest, str):
        base.update({
            "generation": 1,
            "revision": 1,
            "boundary": {
                "id": str(raw.get("id") or "legacy-compact"),
                "coveredThroughSeq": covered_seq,
                "coveredThroughMessageId": covered_id,
                "coveredPrefixDigest": digest,
                "trigger": "migration",
                "sourceRunId": raw.get("sourceRunId"),
                "createdAt": raw.get("updatedAt"),
            },
            "summary": {
                "formatVersion": 1,
                "text": str(summary_text or ""),
                "modelId": None,
                "inputTokens": None,
                "outputTokens": None,
            },
            "stats": copy.deepcopy(raw.get("stats") or {}),
        })
    return base


class ContextStateStore:
    """在 Access Layer 内拥有每会话 compact state 的唯一读写入口。"""

    def __init__(self, storage: StorageBackend | None) -> None:
        self._storage = storage
        self._memory: dict[str, dict[str, Any]] = {}

    async def _key(self, session_id: str) -> str:
        settings = await get_or_init_settings()
        return f"{settings.session_storage_prefix}/{session_id}/context/k_agent.json"

    async def read(self, session_id: str) -> dict[str, Any]:
        if self._storage is None:
            return copy.deepcopy(self._memory.get(session_id, empty_context_state(session_id)))
        key = await self._key(session_id)
        raw = await self._storage.read_json(key)
        normalized = normalize_context_state(session_id, raw)
        if isinstance(raw, dict) and "generation" not in raw:
            # 旧平铺 state 没有 revision；首次读取就在存储锁内升级，后续
            # proposal 才能以 generation/revision 做可靠 CAS。
            upgraded = await self._storage.compare_and_swap_json(
                key, expected_revision=0, payload=normalized
            )
            if not upgraded:
                # 另一 worker/process 已先完成升级；返回它的版本，不能把本地旧
                # 快照伪装成当前 revision。（部署仍限制单 worker，CAS 保留健壮性。）
                normalized = normalize_context_state(
                    session_id, await self._storage.read_json(key)
                )
        return normalized

    async def delete(self, session_id: str) -> None:
        self._memory.pop(session_id, None)
        if self._storage is not None:
            await self._storage.delete(await self._key(session_id))

    async def compare_and_swap(
        self,
        session_id: str,
        *,
        expected_revision: int,
        next_state: dict[str, Any],
    ) -> bool:
        normalized = normalize_context_state(session_id, next_state)
        normalized["revision"] = expected_revision + 1
        if self._storage is None:
            current = self._memory.get(session_id, empty_context_state(session_id))
            if int(current.get("revision", 0)) != expected_revision:
                return False
            self._memory[session_id] = copy.deepcopy(normalized)
            return True
        return await self._storage.compare_and_swap_json(
            await self._key(session_id),
            expected_revision=expected_revision,
            payload=normalized,
        )

    async def validated(
        self, session_id: str, history_records: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """校验前缀；失效时删除派生状态并退回完整历史。"""

        state = await self.read(session_id)
        boundary = state.get("boundary")
        if not isinstance(boundary, dict):
            return state
        seq = boundary.get("coveredThroughSeq")
        digest = boundary.get("coveredPrefixDigest")
        if (
            not isinstance(seq, int)
            or not isinstance(digest, str)
            or projected_prefix_digest(history_records, seq) != digest
        ):
            log_event(
                "context_state_invalidated",
                threadId=session_id,
                generation=state.get("generation", 0),
                reason="prefix_digest_mismatch",
            )
            await self.delete(session_id)
            return empty_context_state(session_id)
        return state

    async def active_view(
        self, session_id: str, history_records: list[dict[str, Any]]
    ) -> tuple[list[ChatMessage], dict[str, Any]]:
        """返回 boundary 后原始消息，并幂等应用已提交的工具正文 replacement。"""

        state = await self.validated(session_id, history_records)
        boundary = state.get("boundary")
        seq = boundary.get("coveredThroughSeq") if isinstance(boundary, dict) else None
        records = (
            [record for record in history_records if int(record.get("seq") or 0) > seq]
            if isinstance(seq, int)
            else history_records
        )
        messages = messages_from_records(records)
        replacements = {
            item.get("messageId"): item
            for item in state.get("toolReplacements", [])
            if isinstance(item, dict)
            and isinstance(item.get("messageId"), str)
            and isinstance(item.get("replacement"), str)
        }
        projected: list[ChatMessage] = []
        for message in messages:
            replacement = replacements.get(message.id)
            digest = (
                "sha256:" + hashlib.sha256(
                    message.content.encode("utf-8")
                ).hexdigest()
            )
            if (
                message.role == "tool"
                and isinstance(replacement, dict)
                and replacement.get("sourceDigest") == digest
            ):
                projected.append(message.model_copy(update={"content": replacement["replacement"]}))
            else:
                projected.append(message)
        return projected, state

    async def commit_compaction(
        self,
        session_id: str,
        history_records: list[dict[str, Any]],
        proposal: dict[str, Any],
        continuation_checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """校验提案并把 summary/boundary/checkpoint 一次 CAS 提交。"""

        current = await self.validated(session_id, history_records)
        expected_generation = proposal.get("expectedGeneration")
        expected_revision = proposal.get("expectedRevision")
        proposal_id = proposal.get("proposalId")
        if current.get("lastProposalId") == proposal_id and proposal_id:
            return current
        if (
            expected_generation != current.get("generation")
            or expected_revision != current.get("revision")
        ):
            raise ContextStateConflict("Context proposal generation/revision is stale")
        boundary_input = proposal.get("boundary")
        message_id = (
            boundary_input.get("coveredThroughMessageId")
            if isinstance(boundary_input, dict)
            else None
        )
        seq = message_seq_index(history_records).get(message_id) if isinstance(message_id, str) else None
        if not isinstance(seq, int):
            raise ContextStateInvalid("Compaction boundary is not durably anchored")
        now = datetime.now(timezone.utc).isoformat()
        boundary = {
            **copy.deepcopy(boundary_input),
            "id": str(boundary_input.get("id") or proposal_id),
            "coveredThroughSeq": seq,
            "coveredPrefixDigest": projected_prefix_digest(history_records, seq),
            "createdAt": str(boundary_input.get("createdAt") or now),
        }
        summary = copy.deepcopy(proposal.get("summary"))
        if not isinstance(summary, dict) or not str(summary.get("text") or "").strip():
            raise ContextStateInvalid("Compaction summary is empty")
        retained_replacements = [
            item for item in proposal.get("toolReplacements", [])
            if isinstance(item, dict)
            and message_seq_index(history_records).get(item.get("messageId"), seq + 1) > seq
        ]
        next_state = {
            **current,
            "generation": int(current.get("generation", 0)) + 1,
            "boundary": boundary,
            "summary": summary,
            "toolReplacements": retained_replacements,
            "workingSet": copy.deepcopy(proposal.get("workingSet") or current.get("workingSet")),
            "failureState": empty_context_state(session_id)["failureState"],
            "pendingContinuation": copy.deepcopy(continuation_checkpoint),
            "lastProposalId": proposal_id,
            "stats": copy.deepcopy(proposal.get("stats") or {}),
            "updatedAt": now,
        }
        if not await self.compare_and_swap(
            session_id,
            expected_revision=int(expected_revision),
            next_state=next_state,
        ):
            raise ContextStateConflict("Context proposal lost its storage CAS")
        return await self.read(session_id)

    async def commit_patch(
        self, session_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """提交不推进 generation 的 microcompact replacement patch。"""

        current = await self.read(session_id)
        if current.get("lastProposalId") == patch.get("proposalId"):
            return current
        expected_revision = patch.get("expectedRevision")
        if expected_revision != current.get("revision"):
            raise ContextStateConflict("Context patch revision is stale")
        merged = {
            item.get("messageId"): item
            for item in current.get("toolReplacements", [])
            if isinstance(item, dict) and isinstance(item.get("messageId"), str)
        }
        for item in patch.get("toolReplacements", []):
            if isinstance(item, dict) and isinstance(item.get("messageId"), str):
                merged[item["messageId"]] = copy.deepcopy(item)
        next_state = {
            **current,
            "toolReplacements": list(merged.values()),
            "lastProposalId": patch.get("proposalId"),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        if not await self.compare_and_swap(
            session_id,
            expected_revision=int(expected_revision),
            next_state=next_state,
        ):
            raise ContextStateConflict("Context patch lost its storage CAS")
        return await self.read(session_id)

    async def clear_pending(self, session_id: str, *, expected_generation: int) -> None:
        """continuation 结束后清理一次性 checkpoint；代次变化则拒绝误清。"""

        current = await self.read(session_id)
        if current.get("generation") != expected_generation:
            raise ContextStateConflict("Continuation generation changed")
        if current.get("pendingContinuation") is None:
            return
        next_state = {**current, "pendingContinuation": None}
        if not await self.compare_and_swap(
            session_id,
            expected_revision=int(current.get("revision", 0)),
            next_state=next_state,
        ):
            raise ContextStateConflict("Continuation cleanup lost its storage CAS")

    async def record_failure(
        self, session_id: str, *, code: str, automatic: bool
    ) -> dict[str, Any]:
        """持久化 compact 失败熔断；自动失败累计三次后禁用。"""

        for _ in range(3):
            current = await self.read(session_id)
            failures = copy.deepcopy(current.get("failureState") or {})
            count = int(failures.get("consecutiveAutoFailures", 0)) + (1 if automatic else 0)
            failures.update({
                "consecutiveAutoFailures": count,
                "autoDisabled": count >= 3,
                "lastFailureCode": code,
            })
            next_state = {**current, "failureState": failures}
            revision = int(current.get("revision", 0))
            if await self.compare_and_swap(
                session_id, expected_revision=revision, next_state=next_state
            ):
                return await self.read(session_id)
        raise ContextStateConflict("Failed to persist context failure state")

    async def clear_failure(self, session_id: str) -> None:
        """配置变化后只清自动熔断，不删除仍然有效的摘要/boundary。"""

        for _ in range(3):
            current = await self.read(session_id)
            if current.get("failureState") == empty_context_state(session_id)["failureState"]:
                return
            next_state = {
                **current,
                "failureState": empty_context_state(session_id)["failureState"],
            }
            revision = int(current.get("revision", 0))
            if await self.compare_and_swap(
                session_id, expected_revision=revision, next_state=next_state
            ):
                return
        raise ContextStateConflict("Failed to clear context failure state")

    @staticmethod
    def public_status(state: dict[str, Any]) -> dict[str, Any]:
        """剥离摘要、哈希、replacement 和 checkpoint，只公开产品状态。"""

        boundary = state.get("boundary") if isinstance(state.get("boundary"), dict) else {}
        failure = state.get("failureState") if isinstance(state.get("failureState"), dict) else {}
        stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
        return {
            "generation": int(state.get("generation", 0)),
            "boundaryId": boundary.get("id"),
            "lastCompactedAt": boundary.get("createdAt"),
            "trigger": boundary.get("trigger"),
            "beforeTokens": stats.get("beforeTokens"),
            "afterTokens": stats.get("afterTokens"),
            "savedTokens": stats.get("savedTokens"),
            "warning": bool(stats.get("warning")),
            "autoDisabled": bool(failure.get("autoDisabled")),
            "consecutiveAutoFailures": int(failure.get("consecutiveAutoFailures", 0)),
            "lastFailureCode": failure.get("lastFailureCode"),
            "pendingContinuation": state.get("pendingContinuation") is not None,
        }
