import assert from "node:assert/strict";
import test from "node:test";
import { parseAgUiSse } from "../src/client/sse-parser.js";

test("SSE parser 跨 chunk 保留完整 frame 和顺序", async () => {
  const encoder = new TextEncoder();
  const chunks = [
    'data: {"type":"TEXT_MESSAGE_START","messageId":"m1",',
    '"role":"assistant"}\n\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m1","delta":"你',
    '好"}\n\ndata: {"type":"TEXT_MESSAGE_END","messageId":"m1"}\n\n',
  ];
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  const events = [];
  for await (const event of parseAgUiSse(new Response(stream))) events.push(event);
  assert.deepEqual(events.map((event) => event.type), ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]);
  assert.equal(events[1]?.type === "TEXT_MESSAGE_CONTENT" && events[1].delta, "你好");
});
