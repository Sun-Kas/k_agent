/**
 * 广场只打 Access Layer /api/marketplace/*，不直连 SkillHub 或 MCP Registry。
 */
import { appConfig } from "../config";

const apiUrl = (path: string) => `${appConfig.apiBaseUrl}${path}`;

export interface MarketplaceFieldMeta {
  key: string;
  kind: "env" | "header";
  description: string;
  required: boolean;
  secret: boolean;
}

export interface MarketplaceItem {
  kind: "mcp" | "skill";
  source: string;
  sourceId: string;
  title: string;
  summary: string;
  version?: string | null;
  iconUrl?: string | null;
  homepage?: string | null;
  ownerName?: string | null;
  changelog?: string | null;
  body?: string | null;
  categories: string[];
  tags: string[];
  stats: { downloads: number | null; installs: number | null; score: number | null };
  officialStatus?: string | null;
  installed: boolean;
  localId?: string | null;
  installPreview?: {
    transport?: string | null;
    command?: string | null;
    args?: string[];
    url?: string | null;
    envKeys?: string[];
    headerKeys?: string[];
    secretKeys?: string[];
    fieldMeta?: MarketplaceFieldMeta[];
    missingEnvKeys?: string[];
    blockedReason?: string | null;
    archive?: string;
    requiresFrontmatter?: string[];
  } | null;
  conflict?: { localId: string; exists: boolean };
}

export interface MarketplaceListing {
  items: MarketplaceItem[];
  page: {
    nextCursor: string | null;
    page: number | null;
    pageSize: number | null;
    total: number | null;
  };
  sourceStatus: "ok" | "degraded" | "unavailable";
  warnings: string[];
}

async function marketplaceFetch(path: string, init?: RequestInit, timeoutMs = 20000): Promise<Response> {
  return fetch(apiUrl(path), { ...init, signal: AbortSignal.timeout(timeoutMs) });
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json() as { detail?: string };
    if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail;
  } catch {
    // ignore
  }
  return `${fallback}（${response.status}）`;
}

export const MARKETPLACE_PAGE_SIZE = 24;

export async function listMarketplaceMcp(query = "", page = 1): Promise<MarketplaceListing> {
  const params = new URLSearchParams({ page: String(page), pageSize: String(MARKETPLACE_PAGE_SIZE) });
  if (query) params.set("q", query);
  const response = await marketplaceFetch(`/api/marketplace/mcp?${params}`);
  if (!response.ok) throw new Error(await readError(response, "无法加载 MCP 广场"));
  return response.json() as Promise<MarketplaceListing>;
}

export async function previewMarketplaceMcp(sourceId: string): Promise<MarketplaceItem> {
  const response = await marketplaceFetch(
    `/api/marketplace/mcp/item/preview?sourceId=${encodeURIComponent(sourceId)}`,
    { method: "POST" }
  );
  if (!response.ok) throw new Error(await readError(response, "无法预览 MCP 安装"));
  return response.json() as Promise<MarketplaceItem>;
}

export async function installMarketplaceMcp(
  sourceId: string,
  body: { id?: string; env?: Record<string, string>; headers?: Record<string, string> }
): Promise<{ ok: boolean; localId: string }> {
  const response = await marketplaceFetch(
    `/api/marketplace/mcp/item/install?sourceId=${encodeURIComponent(sourceId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    },
    120000
  );
  if (!response.ok) throw new Error(await readError(response, "MCP 安装失败"));
  return response.json() as Promise<{ ok: boolean; localId: string }>;
}

export async function listMarketplaceSkills(query = "", page = 1): Promise<MarketplaceListing> {
  const params = new URLSearchParams({ page: String(page), pageSize: String(MARKETPLACE_PAGE_SIZE) });
  if (query) params.set("q", query);
  const response = await marketplaceFetch(`/api/marketplace/skills?${params}`);
  if (!response.ok) throw new Error(await readError(response, "无法加载 Skill 广场"));
  return response.json() as Promise<MarketplaceListing>;
}

export async function previewMarketplaceSkill(slug: string): Promise<MarketplaceItem> {
  const response = await marketplaceFetch(
    `/api/marketplace/skills/${encodeURIComponent(slug)}/preview`,
    { method: "POST" }
  );
  if (!response.ok) throw new Error(await readError(response, "无法预览 Skill 安装"));
  return response.json() as Promise<MarketplaceItem>;
}

export async function installMarketplaceSkill(slug: string): Promise<{ ok: boolean; localId: string; name?: string }> {
  const response = await marketplaceFetch(
    `/api/marketplace/skills/${encodeURIComponent(slug)}/install`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: true })
    },
    120000
  );
  if (!response.ok) throw new Error(await readError(response, "Skill 安装失败"));
  return response.json() as Promise<{ ok: boolean; localId: string; name?: string }>;
}
