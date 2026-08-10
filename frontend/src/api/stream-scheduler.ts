/**
 * SSE 文本流的渲染背压：在可见标签页上等待下一帧再继续读流，避免高频 delta 卡死 UI。
 */
type StreamWindow = Pick<Window, "requestAnimationFrame" | "setTimeout" | "clearTimeout">;
type StreamDocument = Pick<Document, "visibilityState">;

/**
 * 返回可继续消费下一条 TEXT_MESSAGE_CONTENT 的时机。
 * 后台页 rAF 可能永不触发，故直接 resolve，让 React 在页签恢复时一次性应用积压状态。
 */
export function nextRenderOpportunity(
  targetWindow: StreamWindow | undefined = typeof window === "undefined" ? undefined : window,
  targetDocument: StreamDocument | undefined = typeof document === "undefined" ? undefined : document
): Promise<void> {
  if (!targetWindow || !targetDocument || typeof targetWindow.requestAnimationFrame !== "function") {
    return Promise.resolve();
  }

  // 后台标签可能无限挂起 requestAnimationFrame；不要让绘制背压卡住 response reader。
  if (targetDocument.visibilityState !== "visible") {
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      targetWindow.clearTimeout(fallbackTimer);
      resolve();
    };
    // 覆盖：visibility 检查后、下一帧送达前标签被切到后台的竞态。
    const fallbackTimer = targetWindow.setTimeout(finish, 100);
    targetWindow.requestAnimationFrame(finish);
  });
}
