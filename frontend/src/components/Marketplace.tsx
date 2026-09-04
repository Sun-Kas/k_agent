/**
 * 公网 Skill / MCP 市场。侧栏点「技能」或「连接器」直接进入对应目录，页内不再切换。
 */
import { FormEvent, useEffect, useState } from "react";
import {
  installMarketplaceMcp,
  installMarketplaceSkill,
  listMarketplaceMcp,
  listMarketplaceSkills,
  previewMarketplaceMcp,
  previewMarketplaceSkill,
  MARKETPLACE_PAGE_SIZE,
  type MarketplaceFieldMeta,
  type MarketplaceItem,
  type MarketplaceListing
} from "../api/marketplace";

export function Marketplace({
  kind,
  onOpenConfig,
  onInstalled
}: {
  kind: "mcp" | "skill";
  onOpenConfig: () => void;
  onInstalled: () => void;
}) {
  const [query, setQuery] = useState("");
  const [listing, setListing] = useState<MarketplaceListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [preview, setPreview] = useState<MarketplaceItem | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [page, setPage] = useState(1);
  const isMcp = kind === "mcp";

  async function loadPage(targetPage: number, nextQuery = query) {
    setLoading(true);
    try {
      const data = isMcp
        ? await listMarketplaceMcp(nextQuery, targetPage)
        : await listMarketplaceSkills(nextQuery, targetPage);
      setListing({
        ...data,
        page: {
          ...data.page,
          page: data.page.page || targetPage,
          pageSize: data.page.pageSize || MARKETPLACE_PAGE_SIZE
        }
      });
      setPage(data.page.page || targetPage);
      setNotice(
        data.sourceStatus === "unavailable"
          ? { kind: "error", text: data.warnings[0] || "市场暂时不可用，本机会话不受影响" }
          : null
      );
    } catch (error) {
      setListing({ items: [], page: { nextCursor: null, page: targetPage, pageSize: MARKETPLACE_PAGE_SIZE, total: 0 }, sourceStatus: "unavailable", warnings: [] });
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "市场加载失败" });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setQuery("");
    setPreview(null);
    setPage(1);
    void loadPage(1, "");
    // 侧栏切换 kind 时整页按 key 重挂；此处只在首次进入拉数。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind]);

  async function onSearch(event: FormEvent) {
    event.preventDefault();
    setPage(1);
    await loadPage(1, query);
  }

  async function goToPage(targetPage: number) {
    if (targetPage < 1 || targetPage === page || loading) return;
    await loadPage(targetPage);
    document.querySelector(".marketplace-page")?.scrollTo({ top: 0 });
  }

  async function openPreview(item: MarketplaceItem) {
    setNotice(null);
    setDialogError("");
    setSecrets({});
    setPreview(item);
    setPreviewLoading(true);
    try {
      const detail = isMcp
        ? await previewMarketplaceMcp(item.sourceId)
        : await previewMarketplaceSkill(item.sourceId);
      setPreview(detail);
    } catch (error) {
      setPreview(null);
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "预览失败" });
    } finally {
      setPreviewLoading(false);
    }
  }

  function closePreview() {
    if (busy) return;
    setPreview(null);
    setDialogError("");
    setPreviewLoading(false);
  }

  async function confirmInstall() {
    if (!preview) return;
    setBusy(true);
    setDialogError("");
    try {
      if (isMcp) {
        const env: Record<string, string> = {};
        const headers: Record<string, string> = {};
        for (const field of preview.installPreview?.fieldMeta ?? []) {
          const value = secrets[field.key]?.trim();
          if (!value) continue;
          if (field.kind === "header") headers[field.key] = value;
          else env[field.key] = value;
        }
        await installMarketplaceMcp(preview.sourceId, { env, headers });
      } else {
        await installMarketplaceSkill(preview.sourceId);
      }
      setPreview(null);
      onInstalled();
      await loadPage(page, query);
      setNotice({ kind: "ok", text: "已写入本机目录，不会自动勾进当前会话。" });
    } catch (error) {
      setDialogError(error instanceof Error ? error.message : "安装失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="marketplace-page">
      <section className="marketplace-section">
        <header className="work-page-header">
          <div>
            <small>{isMcp ? "MODELSCOPE MCP" : "SKILLHUB"}</small>
            <h1>{isMcp ? "连接器" : "技能"}</h1>
            <p>{isMcp ? "从魔搭 MCP 广场安装连接器。需要 API Key 时会先弹出表单。" : "从 SkillHub 安装 Skill，校验通过后写入本机目录。"}</p>
          </div>
        </header>
        <form className="marketplace-search" onSubmit={onSearch}>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={isMcp ? "按名称搜索，例如 高德 或 fetch" : "搜索技能，例如 PDF"}
          />
          <button className="marketplace-search-submit" type="submit">搜索</button>
        </form>
        {notice && <p className={`marketplace-banner ${notice.kind}`}>{notice.text}</p>}
        {loading ? <div className="config-loading">正在读取市场…</div> : (
          <div className="marketplace-grid">
            {(listing?.items ?? []).map((item) => (
              <article key={`${item.source}:${item.sourceId}`} className="marketplace-card">
                <header>
                  <div className="marketplace-card-title">
                    <MarketplaceMark url={item.iconUrl} title={item.title || item.sourceId} />
                    <strong>{item.title || item.sourceId}</strong>
                  </div>
                  {item.installed ? <b>已安装</b> : null}
                </header>
                <p>{item.summary || "暂无简介"}</p>
                <small>{item.version || "latest"} · {item.sourceId}</small>
                {item.installed ? (
                  <button className="marketplace-card-action" type="button" onClick={onOpenConfig}>打开配置</button>
                ) : (
                  <button className="marketplace-card-action" type="button" onClick={() => void openPreview(item)}>预览安装</button>
                )}
              </article>
            ))}
            {!listing?.items.length && <p>没有匹配的条目。</p>}
          </div>
        )}
        {!loading && listing ? (
          <MarketplacePager
            page={page}
            listing={listing}
            onPage={goToPage}
          />
        ) : null}
      </section>
      {preview && (
        <div className="marketplace-dialog-backdrop" role="presentation" onClick={closePreview}>
          <section
            className="marketplace-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="marketplace-install-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header>
              <div className="marketplace-dialog-heading">
                <MarketplaceMark url={preview.iconUrl} title={preview.title || preview.sourceId} large />
                <div>
                  <p className="marketplace-dialog-kicker">{isMcp ? "MCP 详情" : "Skill 详情"}</p>
                  <h3 id="marketplace-install-title">{preview.title || preview.sourceId}</h3>
                  <p className="marketplace-dialog-meta">
                    {[preview.version || "latest", preview.sourceId, preview.ownerName].filter(Boolean).join(" · ")}
                  </p>
                </div>
              </div>
            </header>
            <div className="marketplace-dialog-body">
              {previewLoading ? <p className="marketplace-dialog-hint">正在读取详情…</p> : (
                <MarketplacePreviewDetail item={preview} isMcp={isMcp} />
              )}
              <p className="marketplace-dialog-hint">
                {isMcp
                  ? "校验通过后写入本机 Catalog，需要密钥时请先填下方字段。不会自动勾进当前会话。"
                  : "将下载 zip 并校验 SKILL.md，写入本机目录后不会自动勾进当前会话。"}
              </p>
              {preview.conflict?.exists && (
                <p className="marketplace-dialog-alert">本机已有 {preview.conflict.localId}，这一版不会覆盖。</p>
              )}
              {preview.installPreview?.blockedReason && (
                <p className="marketplace-dialog-alert">无法自动安装：{preview.installPreview.blockedReason}</p>
              )}
              {(preview.installPreview?.fieldMeta ?? []).map((field) => (
                <SecretField key={field.key} field={field} value={secrets[field.key] ?? ""} onChange={(value) => setSecrets((current) => ({ ...current, [field.key]: value }))} />
              ))}
              {dialogError && <p className="marketplace-dialog-alert">{dialogError}</p>}
            </div>
            <footer>
              <button className="marketplace-btn-ghost" type="button" disabled={busy} onClick={closePreview}>取消</button>
              <button
                className="marketplace-btn-primary"
                type="button"
                disabled={busy || Boolean(preview.installPreview?.blockedReason) || Boolean(preview.conflict?.exists)}
                onClick={() => void confirmInstall()}
              >
                {busy ? "正在安装…" : "确认安装"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </main>
  );
}

function MarketplaceMark({
  url,
  title,
  large
}: {
  url?: string | null;
  title: string;
  large?: boolean;
}) {
  const [broken, setBroken] = useState(false);
  useEffect(() => {
    setBroken(false);
  }, [url]);
  const letter = Array.from(title.trim() || "?")[0]?.toUpperCase() || "?";
  return (
    <span className={`marketplace-mark${large ? " large" : ""}`} aria-hidden="true">
      {url && !broken ? (
        <img src={url} alt="" referrerPolicy="no-referrer" onError={() => setBroken(true)} />
      ) : letter}
    </span>
  );
}

function MarketplacePreviewDetail({ item, isMcp }: { item: MarketplaceItem; isMcp: boolean }) {
  const preview = item.installPreview;
  const command = preview?.command
    ? [preview.command, ...(preview.args ?? [])].join(" ")
    : null;
  const rows: Array<[string, string]> = [];
  if (isMcp) {
    if (preview?.transport) rows.push(["传输", preview.transport]);
    if (command) rows.push(["启动命令", command]);
    if (preview?.url) rows.push(["远程地址", preview.url]);
    if (item.officialStatus) rows.push(["注册表状态", item.officialStatus]);
    if (item.localId) rows.push(["将写入", item.localId]);
    if (preview?.missingEnvKeys?.length) rows.push(["必填配置", preview.missingEnvKeys.join("、")]);
  } else {
    if (item.ownerName) rows.push(["作者", item.ownerName]);
    if (item.categories.length) rows.push(["分类", item.categories.join("、")]);
    if (item.localId) rows.push(["将写入", item.localId]);
    const downloads = item.stats.downloads;
    const installs = item.stats.installs;
    if (downloads != null || installs != null) {
      rows.push(["用量", [downloads != null ? `下载 ${downloads}` : "", installs != null ? `安装 ${installs}` : ""].filter(Boolean).join(" · ")]);
    }
  }
  if (item.homepage) rows.push(["主页", item.homepage]);

  return (
    <div className="marketplace-preview-detail">
      {item.summary ? <p className="marketplace-dialog-summary">{item.summary}</p> : null}
      {item.tags.length ? (
        <p className="marketplace-chips">{item.tags.map((tag) => <span key={tag}>{tag}</span>)}</p>
      ) : null}
      {rows.length ? (
        <dl className="marketplace-dl">
          {rows.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                {label === "主页" || label === "远程地址" ? (
                  <a href={value} target="_blank" rel="noreferrer">{value}</a>
                ) : value}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      {!isMcp && item.changelog ? <p className="marketplace-changelog">更新说明：{item.changelog}</p> : null}
      {item.body ? (
        <section className="marketplace-skill-body">
          <h4>{isMcp ? "说明" : "SKILL.md"}</h4>
          <pre>{item.body}</pre>
        </section>
      ) : (!isMcp ? (
        <section className="marketplace-skill-body">
          <h4>SKILL.md</h4>
          <p>暂时读不到正文，安装时仍会下载并校验 zip。</p>
        </section>
      ) : null)}
    </div>
  );
}

function MarketplacePager({
  page,
  listing,
  onPage
}: {
  page: number;
  listing: MarketplaceListing;
  onPage: (page: number) => void;
}) {
  const pageSize = listing.page.pageSize || MARKETPLACE_PAGE_SIZE;
  const total = listing.page.total;
  const totalPages = typeof total === "number" && total >= 0 ? Math.max(1, Math.ceil(total / pageSize)) : null;
  const hasNext = totalPages !== null ? page < totalPages : listing.items.length >= pageSize;
  const hasPrev = page > 1;
  const numbers = totalPages !== null
    ? pageNumbers(page, totalPages)
    : [page];

  return (
    <nav className="marketplace-pager" aria-label="分页">
      <p>
        {totalPages !== null
          ? `第 ${page} / ${totalPages} 页 · 共 ${total} 条`
          : `第 ${page} 页`}
      </p>
      <div>
        <button type="button" disabled={!hasPrev} onClick={() => onPage(page - 1)}>上一页</button>
        {numbers.map((item, index) => (
          item === "…" ? (
            <span key={`gap-${index}`}>…</span>
          ) : (
            <button
              key={item}
              type="button"
              className={item === page ? "active" : undefined}
              onClick={() => onPage(item)}
            >
              {item}
            </button>
          )
        ))}
        <button type="button" disabled={!hasNext} onClick={() => onPage(page + 1)}>下一页</button>
      </div>
    </nav>
  );
}

function pageNumbers(current: number, total: number): Array<number | "…"> {
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  const items = new Set([1, total, current, current - 1, current + 1]);
  const ordered = [...items].filter((item) => item >= 1 && item <= total).sort((a, b) => a - b);
  const result: Array<number | "…"> = [];
  for (const item of ordered) {
    const prev = result[result.length - 1];
    if (typeof prev === "number" && item - prev > 1) result.push("…");
    result.push(item);
  }
  return result;
}

function SecretField({
  field,
  value,
  onChange
}: {
  field: MarketplaceFieldMeta;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="config-field">
      <span>
        <strong>{field.key}{field.required ? " *" : ""}</strong>
        {field.description && <small>{field.description}</small>}
      </span>
      <input
        type={field.secret ? "password" : "text"}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
      />
    </label>
  );
}
