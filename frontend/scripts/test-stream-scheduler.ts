import assert from "node:assert/strict";
import { nextRenderOpportunity } from "../src/api/stream-scheduler";

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

console.log("stream scheduler regression tests passed");
