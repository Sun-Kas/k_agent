import assert from "node:assert/strict";
import { nextRenderOpportunity, yieldAfterStreamBatch } from "../src/api/stream-scheduler";

let backgroundFrameRequested = false;
const backgroundWindow = {
  requestAnimationFrame: () => {
    backgroundFrameRequested = true;
    return 1;
  },
  setTimeout,
  clearTimeout
};

await nextRenderOpportunity(
  backgroundWindow as unknown as Window,
  { visibilityState: "hidden" } as Document
);
assert.equal(backgroundFrameRequested, false);

let foregroundFrameRequested = false;
let foregroundTimerCleared = false;
const foregroundWindow = {
  requestAnimationFrame: (callback: FrameRequestCallback) => {
    foregroundFrameRequested = true;
    callback(0);
    return 1;
  },
  setTimeout: () => 1,
  clearTimeout: () => {
    foregroundTimerCleared = true;
  }
};

await nextRenderOpportunity(
  foregroundWindow as unknown as Window,
  { visibilityState: "visible" } as Document
);
assert.equal(foregroundFrameRequested, true);
assert.equal(foregroundTimerCleared, true);

// Approval/tool batches can contain no text delta. A dispatched non-text batch
// must still yield one paint, while an empty reader batch must not yield.
let approvalPaints = 0;
const approvalWindow = {
  requestAnimationFrame: (callback: FrameRequestCallback) => {
    approvalPaints += 1;
    callback(0);
    return approvalPaints;
  },
  setTimeout: () => 1,
  clearTimeout: () => undefined
};
const visibleDocument = { visibilityState: "visible" };
await yieldAfterStreamBatch(
  true,
  approvalWindow as unknown as Window,
  visibleDocument as Document
);
assert.equal(approvalPaints, 1);
await yieldAfterStreamBatch(
  false,
  approvalWindow as unknown as Window,
  visibleDocument as Document
);
assert.equal(approvalPaints, 1);

console.log("stream scheduler regression tests passed");
