import assert from "node:assert/strict";

import { bindApprovalRunToAssistant } from "../src/live-approval";
import type { ChatMessage } from "../src/types";

const messages: ChatMessage[] = [
  { id: "user-1", role: "user", content: "run", createdAt: "2026-08-11T00:00:00Z" },
  { id: "pending-1", role: "assistant", content: "", createdAt: "2026-08-11T00:00:01Z" }
];

const bound = bindApprovalRunToAssistant(messages, "run-1", "pending-1");
assert.equal(bound[1].meta?.runId, "run-1");
assert.notEqual(bound, messages);

// Repeated CUSTOM approval_request frames must not clone or mutate the host row.
assert.equal(bindApprovalRunToAssistant(bound, "run-1", "pending-1"), bound);

// Never steal an assistant row that already belongs to another run.
const occupied = [{ ...messages[1], meta: { runId: "run-old" } }];
assert.equal(bindApprovalRunToAssistant(occupied, "run-new", null), occupied);

console.log("live approval host regression tests passed");

