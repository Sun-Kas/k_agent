export class AccessLayerError extends Error {
  readonly status: number | undefined;
  readonly code: "network" | "http" | "protocol" | "timeout";

  constructor(
    message: string,
    options: { status?: number; code?: AccessLayerError["code"]; cause?: unknown } = {},
  ) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "AccessLayerError";
    this.status = options.status;
    this.code = options.code ?? "http";
  }
}

export async function responseError(response: Response, fallback: string): Promise<AccessLayerError> {
  let detail = "";
  try {
    const payload = await response.json() as { detail?: unknown };
    if (typeof payload.detail === "string") detail = payload.detail;
    if (Array.isArray(payload.detail)) {
      detail = payload.detail
        .map((item) => item && typeof item === "object" && "msg" in item ? String(item.msg) : String(item))
        .join("；");
    }
  } catch {
    // 反向代理或网络层可能返回非 JSON，保留状态码作为稳定诊断信息。
  }
  return new AccessLayerError(
    detail ? `${fallback}：${detail}` : `${fallback}（${response.status}）`,
    { status: response.status, code: "http" },
  );
}
