from __future__ import annotations

from datetime import datetime, timezone
import unittest

from backend.api.schemas import ChatMessage
from access_layer.schemas import ModelsConfigUpdate
from backend.context import (
    ToolResultPolicy,
    build_context_plan,
    calculate_context_budget,
    microcompact,
)


def message(index: int, role: str = "user", size: int = 1000) -> ChatMessage:
    return ChatMessage(
        id=f"m-{index}", role=role,
        content=f"message {index} " + ("x" * size),
        createdAt=datetime.now(timezone.utc),
    )


def tool_round(index: int, result_count: int = 1) -> list[dict]:
    calls = [
        {
            "id": f"c-{index}-{result}", "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }
        for result in range(result_count)
    ]
    return [
        {"role": "assistant", "content": None, "tool_calls": calls},
        *[
            {
                "role": "tool", "tool_call_id": call["id"],
                "_message_id": f"m-{call['id']}", "content": "x" * 5_000,
            }
            for call in calls
        ],
    ]


class ContextManagerTests(unittest.TestCase):
    def test_initial_plan_never_compacts_or_drops_messages(self) -> None:
        messages = [
            message(index, "user" if index % 2 == 0 else "assistant", 4000)
            for index in range(14)
        ]
        plan = build_context_plan(
            messages,
            system_prompt="system",
            user_context={"memory": "memory"},
            model_config={
                "contextWindow": 20_000,
                "maxOutputTokens": 1_000,
                "contextSafetyTokens": 1_000,
            },
        )
        self.assertEqual([item.id for item in plan.messages], [item.id for item in messages])
        self.assertEqual(plan.summary, "")

    def test_budget_uses_provider_usage_and_exposes_all_thresholds(self) -> None:
        decision = calculate_context_budget(
            model_config={
                "contextWindow": 100_000,
                "maxOutputTokens": 8_000,
                "contextSafetyTokens": 4_000,
            },
            messages=[], latest_input_usage=82_500,
        )
        self.assertEqual(decision.budget.estimated_input, 82_500)
        self.assertEqual(decision.budget.auto_compact_threshold, 82_000)
        self.assertEqual(decision.budget.hard_request_threshold, 89_000)
        self.assertTrue(decision.needs_compact)

    def test_provider_usage_still_counts_new_tool_growth(self) -> None:
        decision = calculate_context_budget(
            model_config={
                "contextWindow": 100_000, "maxOutputTokens": 8_000,
                "contextSafetyTokens": 4_000,
            },
            messages=[{"role": "tool", "content": "x" * 4_000}],
            latest_input_usage=80_000,
            usage_baseline_estimate=100,
        )
        self.assertGreater(decision.budget.estimated_input, 80_000)

    def test_budget_rejects_impossible_model_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "contextWindow"):
            calculate_context_budget(
                model_config={
                    "contextWindow": 12_000,
                    "maxOutputTokens": 4_000,
                    "contextSafetyTokens": 1_000,
                }, messages=[],
            )

    def test_microcompact_protects_two_complete_tool_rounds(self) -> None:
        messages = [*tool_round(0), *tool_round(1, result_count=3), *tool_round(2)]
        result = microcompact(
            messages,
            policies={"Read": ToolResultPolicy("rerunnable", 300)},
            keep_recent_rounds=2,
        )
        tool_messages = [item for item in result.messages if item["role"] == "tool"]
        self.assertIn("[Older Read output cleared", tool_messages[0]["content"])
        self.assertTrue(all(item["content"] == "x" * 5_000 for item in tool_messages[1:]))
        self.assertEqual(len(result.replacements), 1)

    def test_unknown_tool_defaults_to_retain(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "c-0", "type": "function",
                    "function": {"name": "mcp__server__unknown", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "c-0", "content": "secret" * 100},
            *tool_round(1), *tool_round(2),
        ]
        result = microcompact(messages, policies={})
        self.assertEqual(result.messages[1]["content"], "secret" * 100)

    def test_compact_model_must_be_an_enabled_catalog_entry(self) -> None:
        base = {
            "model": "provider", "baseUrl": "https://example.test/v1",
            "contextWindow": 128_000, "maxOutputTokens": 8_192,
            "contextSafetyTokens": 4_096,
        }
        with self.assertRaisesRegex(ValueError, "enabled model"):
            ModelsConfigUpdate.model_validate({"models": [
                {"id": "main", "name": "Main", **base, "compactModelId": "compact"},
                {"id": "compact", "name": "Compact", **base, "enabled": False},
            ]})


if __name__ == "__main__":
    unittest.main()
