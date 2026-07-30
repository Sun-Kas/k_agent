type StreamWindow = Pick<Window, "requestAnimationFrame" | "setTimeout" | "clearTimeout">;
type StreamDocument = Pick<Document, "visibilityState">;

export function nextRenderOpportunity(
  targetWindow: StreamWindow | undefined = typeof window === "undefined" ? undefined : window,
  targetDocument: StreamDocument | undefined = typeof document === "undefined" ? undefined : document
): Promise<void> {
  if (!targetWindow || !targetDocument || typeof targetWindow.requestAnimationFrame !== "function") {
    return Promise.resolve();
  }

  // Background tabs may suspend requestAnimationFrame indefinitely. Do not let
  // painting backpressure stop the response reader: React can apply the queued
  // state immediately when this page becomes visible again.
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
    // The fallback also covers a tab becoming hidden between the visibility
    // check above and the browser delivering its next animation frame.
    const fallbackTimer = targetWindow.setTimeout(finish, 100);
    targetWindow.requestAnimationFrame(finish);
  });
}
