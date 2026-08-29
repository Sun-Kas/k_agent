import type { AccessLayerClient } from "../client/access-layer-client.js";
import { AccessLayerError } from "../client/errors.js";
import type { AgUiEvent, AgUiRunInput } from "../protocol/index.js";

export interface RunResult {
  events: AgUiEvent[];
  text: string;
  interrupted: boolean;
  failed: boolean;
}

/**
 * 消费一次 AG-UI run。事件回调严格按网络到达顺序执行，不做批量重排。
 * AbortSignal 只表示客户端断开；stop 与 cancel 必须先调用各自的服务端 API。
 */
export async function consumeRun(
  client: AccessLayerClient,
  input: AgUiRunInput,
  options: { signal?: AbortSignal; onEvent?: (event: AgUiEvent) => void } = {},
): Promise<RunResult> {
  const events: AgUiEvent[] = [];
  let text = "";
  let interrupted = false;
  let failed = false;
  let terminalEventSeen = false;
  for await (const event of client.streamRun(input, options.signal)) {
    events.push(event);
    if (event.type === "TEXT_MESSAGE_CONTENT") text += event.delta;
    if (event.type === "RUN_FINISHED") {
      terminalEventSeen = true;
      if (event.outcome?.type === "interrupt") interrupted = true;
    }
    if (event.type === "RUN_ERROR") {
      terminalEventSeen = true;
      failed = true;
    }
    options.onEvent?.(event);
  }
  if (!terminalEventSeen) {
    throw new AccessLayerError("Agent 事件流在终态事件前结束", { code: "protocol" });
  }
  return { events, text, interrupted, failed };
}
