import assert from "node:assert/strict";
import { shouldInterruptForTranscript } from "../src/voice-interruption";

assert.equal(shouldInterruptForTranscript("", "这是助手正在朗读的内容"), false);
assert.equal(shouldInterruptForTranscript("这", "这是助手正在朗读的内容"), false);
assert.equal(shouldInterruptForTranscript("助手正在朗读", "这是助手正在朗读的内容。"), false);
assert.equal(shouldInterruptForTranscript("等一下，我要补充", "这是助手正在朗读的内容。"), true);
assert.equal(shouldInterruptForTranscript("please stop", "Here is the result you requested."), true);
assert.equal(shouldInterruptForTranscript("继续", ""), true);

console.log("voice interruption regression tests passed");
