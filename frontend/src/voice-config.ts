import type { VoiceStyleId } from "./types";

const VOICE_CONFIG_STORAGE_KEY = "k-agent-voice-config";

export type VoiceConfig = {
  voiceURI: string;
  style: VoiceStyleId;
  rate: number;
  pitch: number;
  volume: number;
};

export const VOICE_STYLES: ReadonlyArray<{
  id: VoiceStyleId;
  name: string;
  description: string;
  preview: string;
}> = [
  { id: "natural", name: "自然对话", description: "平稳、清楚，适合日常交流", preview: "你好，我会用自然清楚的方式和你交流。" },
  { id: "warm", name: "温和亲切", description: "更柔和、更有耐心", preview: "你好，很高兴听到你的声音，我们慢慢聊。" },
  { id: "lively", name: "活力清晰", description: "节奏轻快，表达更有精神", preview: "你好，让我们马上开始，看看今天要解决什么问题。" },
  { id: "professional", name: "专业简洁", description: "重点明确，减少铺垫", preview: "你好，我会直接说明重点，并给出清晰可执行的建议。" },
  { id: "storytelling", name: "叙事表达", description: "语句舒展，适合讲解与故事", preview: "你好，我们从事情的起点说起，一步一步把它讲清楚。" }
];

export const DEFAULT_VOICE_CONFIG: VoiceConfig = {
  voiceURI: "",
  style: "natural",
  rate: 1,
  pitch: 1,
  volume: 1
};

const STYLE_IDS = new Set<VoiceStyleId>(VOICE_STYLES.map((style) => style.id));

function clampNumber(value: unknown, minimum: number, maximum: number, fallback: number) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.min(maximum, Math.max(minimum, value))
    : fallback;
}

export function normalizeVoiceConfig(value: unknown): VoiceConfig {
  const candidate = value && typeof value === "object" ? value as Partial<VoiceConfig> : {};
  return {
    // Keep an unavailable voice URI because browser catalogs can arrive later via voiceschanged.
    voiceURI: typeof candidate.voiceURI === "string" ? candidate.voiceURI : "",
    style: typeof candidate.style === "string" && STYLE_IDS.has(candidate.style as VoiceStyleId)
      ? candidate.style as VoiceStyleId
      : DEFAULT_VOICE_CONFIG.style,
    rate: clampNumber(candidate.rate, 0.7, 1.4, DEFAULT_VOICE_CONFIG.rate),
    pitch: clampNumber(candidate.pitch, 0.7, 1.3, DEFAULT_VOICE_CONFIG.pitch),
    volume: clampNumber(candidate.volume, 0.2, 1, DEFAULT_VOICE_CONFIG.volume)
  };
}

export function readVoiceConfig(): VoiceConfig {
  try {
    const raw = localStorage.getItem(VOICE_CONFIG_STORAGE_KEY);
    return raw ? normalizeVoiceConfig(JSON.parse(raw)) : { ...DEFAULT_VOICE_CONFIG };
  } catch {
    // Corrupted or privacy-restricted storage must not prevent chat from loading.
    return { ...DEFAULT_VOICE_CONFIG };
  }
}

export function writeVoiceConfig(value: VoiceConfig): VoiceConfig {
  const normalized = normalizeVoiceConfig(value);
  localStorage.setItem(VOICE_CONFIG_STORAGE_KEY, JSON.stringify(normalized));
  return normalized;
}
