import { AccessLayerError } from "./errors.js";
import type { AgUiEvent } from "../protocol/index.js";

function parseFrame(frame: string): AgUiEvent | null {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  try {
    return JSON.parse(data) as AgUiEvent;
  } catch (error) {
    throw new AccessLayerError("Access Layer 返回了无法解析的 SSE 事件", {
      code: "protocol",
      cause: error,
    });
  }
}

/**
 * 网络块和 SSE frame 没有一一对应关系：一个 frame 可能跨多个 chunk，
 * 一个 chunk 也可能包含多个 frame。解析器只在空行边界后分发完整事件，
 * 从而保持 Access Layer 的原始到达顺序。
 */
export async function* parseAgUiSse(response: Response): AsyncGenerator<AgUiEvent> {
  const reader = response.body?.getReader();
  if (!reader) {
    throw new AccessLayerError("当前运行环境无法读取 SSE response body", { code: "protocol" });
  }

  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const event = parseFrame(frame);
      if (event) yield event;
    }
    if (done) {
      const finalEvent = parseFrame(buffer);
      if (finalEvent) yield finalEvent;
      return;
    }
  }
}
