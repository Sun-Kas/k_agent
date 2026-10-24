import { readFile, mkdir, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export interface ClientPreferences {
  theme: string;
  fontSizeDelta: number;
  sidebar: boolean;
  inspector: boolean;
  /** 旧配置文件里的桌宠字段。CLI 不再绘制桌宠，读入时只为兼容保留。 */
  pet: { enabled: boolean; petId: string; motion: boolean; size: number; opacity: number; edge: "auto" | "left" | "right"; action: string; offset: number };
  /** 旧配置文件里的朗读字段。CLI 不再朗读，读入时只为兼容保留。 */
  voice: { voiceURI: string; rate: number; pitch: number; volume: number; autoEnabled: boolean };
}
export const DEFAULT_PREFERENCES: ClientPreferences = {
  theme: "blueprint", fontSizeDelta: 0, sidebar: false, inspector: false,
  pet: { enabled: true, petId: "yuexinmiao1", motion: true, size: 118, opacity: 0.96, edge: "auto", action: "idle", offset: 0 },
  voice: { voiceURI: "", rate: 1, pitch: 1, volume: 1, autoEnabled: false },
};
export function normalizePreferences(value: unknown): ClientPreferences {
  const input = value && typeof value === "object" ? value as Partial<ClientPreferences> : {};
  const number = (v: unknown, low: number, high: number, fallback: number) => typeof v === "number" && Number.isFinite(v) ? Math.min(high, Math.max(low, v)) : fallback;
  return { ...DEFAULT_PREFERENCES, ...input,
    theme: ["snow", "ink", "blueprint", "forest", "ocean", "sand", "dusk"].includes(input.theme ?? "") ? input.theme! : "blueprint",
    fontSizeDelta: number(input.fontSizeDelta, -2, 6, 0),
    pet: { ...DEFAULT_PREFERENCES.pet, ...input.pet, size: number(input.pet?.size, 82, 168, 118), opacity: number(input.pet?.opacity, 0.45, 1, 0.96), offset: number(input.pet?.offset, 0, 120, 0) },
    voice: { ...DEFAULT_PREFERENCES.voice, ...input.voice, rate: number(input.voice?.rate, 0.7, 1.4, 1), pitch: number(input.voice?.pitch, 0.7, 1.3, 1), volume: number(input.voice?.volume, 0.2, 1, 1) },
  };
}
export async function readPreferences(): Promise<ClientPreferences> {
  try { return normalizePreferences(JSON.parse(await readFile(join(homedir(), ".config/k-agent-cli/preferences.json"), "utf8"))); }
  catch (error) { if (error instanceof SyntaxError || (error as NodeJS.ErrnoException).code === "ENOENT") return structuredClone(DEFAULT_PREFERENCES); throw error; }
}
export async function savePreferences(value: ClientPreferences): Promise<ClientPreferences> {
  const normalized = normalizePreferences(value);
  const dir = join(homedir(), ".config/k-agent-cli");
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await writeFile(join(dir, "preferences.json"), JSON.stringify(normalized), { mode: 0o600 });
  return normalized;
}
