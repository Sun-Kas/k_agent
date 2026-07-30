from __future__ import annotations

from datetime import datetime, timezone
import unittest

from backend.api.schemas import ChatMessage
from backend.context import (
    build_context_plan,
    estimate_message_tokens,
    prune_old_tool_outputs,
)


def message(index: int, role: str = "user", size: int = 1000) -> ChatMessage:
    return ChatMessage(
        id=f"m-{index}",
        role=role,
        content=f"message {index} " + ("x" * size),
        createdAt=datetime.now(timezone.utc),
    )


class ContextManagerTests(unittest.TestCase):
    def test_auto_compaction_preserves_recent_messages(self) -> None:
        messages = [message(index, "user" if index % 2 == 0 else "assistant", 4000) for index in range(14)]
        plan = build_context_plan(
            messages,
            system_prompt="system",
            user_context={"memory": "memory"},
            model_config={
                "contextWindow": 12_000,
                "maxOutputTokens": 1_000,
                "contextSafetyTokens": 1_000,
            },
        )
        self.assertTrue(plan.auto_compacted)
        self.assertGreater(len(plan.compacted_message_ids), 0)
        self.assertEqual(plan.messages[-1].id, "m-13")
        self.assertIn("Compacted conversation", plan.summary)
        self.assertLess(estimate_message_tokens(plan.messages), estimate_message_tokens(messages))

    def test_existing_compacted_messages_stay_out_of_active_context(self) -> None:
        messages = [message(index, size=20) for index in range(8)]
        plan = build_context_plan(
            messages,
            system_prompt="system",
            user_context={},
            model_config={},
            existing_summary="prior summary",
            compacted_message_ids=["m-0", "m-1"],
        )
        self.assertEqual([item.id for item in plan.messages], [f"m-{index}" for index in range(2, 8)])
        self.assertEqual(plan.summary, "prior summary")

    def test_manual_compaction_works_for_short_conversations(self) -> None:
        messages = [message(index, size=20) for index in range(5)]
        plan = build_context_plan(
            messages,
            system_prompt="system",
            user_context={},
            model_config={},
            force_compact=True,
        )
        self.assertEqual([item.id for item in plan.messages], ["m-3", "m-4"])
        self.assertEqual(plan.compacted_message_ids, ["m-0", "m-1", "m-2"])

    def test_old_tool_outputs_are_pruned_before_recent_results(self) -> None:
        messages = [
            {"role": "tool", "content": "a" * 200},
            {"role": "assistant", "content": "continue"},
            {"role": "tool", "content": "b" * 200},
            {"role": "tool", "content": "c" * 200},
        ]
        pruned = prune_old_tool_outputs(messages, max_tool_chars=450, keep_recent=2)
        self.assertIn("cleared from active context", pruned[0]["content"])
        self.assertEqual(pruned[2]["content"], "b" * 200)
        self.assertEqual(pruned[3]["content"], "c" * 200)


if __name__ == "__main__":
    unittest.main()
