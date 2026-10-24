/** 容忍旧值和损坏的本地设置，置顶顺序与项目接口的返回顺序相互独立。 */
export function parseProjectPins(value: string | null): string[] {
  try {
    const parsed: unknown = JSON.parse(value ?? "[]");
    return Array.isArray(parsed) ? [...new Set(parsed.filter((id): id is string => typeof id === "string" && id.length > 0))] : [];
  } catch { return []; }
}

export function toggleProjectPin(pins: string[], projectId: string): string[] {
  return pins.includes(projectId) ? pins.filter((id) => id !== projectId) : [...pins, projectId];
}
