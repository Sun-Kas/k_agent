import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { MarkdownContent } from "./MarkdownContent";

export type ContentStageItem = {
  id: string;
  title: string;
  subtitle?: string;
  badge?: string;
  badgeTone?: string;
  content: string;
  footnote?: string;
};

type ContentStageProps = {
  eyebrow?: string;
  title?: string;
  items: ContentStageItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  emptyTitle?: string;
  emptyHint?: string;
  footer?: ReactNode;
  /** When true, skip outer aside chrome so a parent panel can own the header. */
  embedded?: boolean;
  /** Load a workspace-relative path for HTML asset inlining (CSS/JS). */
  resolveFile?: (path: string) => Promise<string | null>;
};

type PreviewKind =
  | "markdown"
  | "html"
  | "json"
  | "csv"
  | "code"
  | "svg"
  | "text"
  | "empty"
  | "binary";

const PLACEHOLDER_CONTENT = new Set(["选择后加载预览…", "读取失败"]);

export function ContentStage({
  eyebrow = "DELIVERABLES",
  title = "生成内容",
  items,
  selectedId,
  onSelect,
  emptyTitle = "暂无生成内容",
  emptyHint = "任务协作产出的文档与成果会出现在这里",
  footer,
  embedded = false,
  resolveFile
}: ContentStageProps) {
  // Browse = full tree on open; preview only after an explicit file click.
  const [mode, setMode] = useState<"browse" | "preview">("browse");
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(() => new Set());
  const [openTabs, setOpenTabs] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const knownDirsRef = useRef<Set<string>>(new Set());
  const workspaceIdsRef = useRef("");
  const treePathsKey = useMemo(
    () => items.map((item) => `${item.id}:${item.badge ?? ""}`).sort().join("\0"),
    [items]
  );
  const workspaceIdsKey = useMemo(
    () => items.map((item) => item.id).sort().join("\0"),
    [items]
  );
  const fileTree = useMemo(() => buildFileTree(items), [treePathsKey]);
  const visibleTree = useMemo(() => filterFileTree(fileTree, query), [fileTree, query]);
  const activeId = mode === "preview"
    ? (previewId && items.some((item) => item.id === previewId) ? previewId : selectedId)
    : null;
  const selected = activeId
    ? items.find((item) => item.id === activeId) ?? null
    : null;

  useEffect(() => {
    // New folders start expanded; existing collapse choices survive refreshes.
    const dirs = collectDirPaths(fileTree);
    setExpandedDirs((current) => {
      const next = new Set<string>();
      for (const dir of dirs) {
        if (!knownDirsRef.current.has(dir) || current.has(dir)) next.add(dir);
      }
      knownDirsRef.current = dirs;
      return next;
    });
  }, [treePathsKey, fileTree]);

  useEffect(() => {
    if (!activeId) return;
    const path = normalizeWorkspacePath(
      items.find((item) => item.id === activeId)?.subtitle || activeId
    );
    if (!path || path.startsWith("artifact:")) return;
    const parts = path.split("/").filter(Boolean);
    if (parts.length <= 1) return;
    setExpandedDirs((current) => {
      const next = new Set(current);
      let prefix = "";
      for (const part of parts.slice(0, -1)) {
        prefix = prefix ? `${prefix}/${part}` : part;
        next.add(prefix);
      }
      return next;
    });
  }, [activeId, items]);

  useEffect(() => {
    if (!items.length) {
      workspaceIdsRef.current = "";
      setMode("browse");
      setPreviewId(null);
      setOpenTabs([]);
      setQuery("");
      return;
    }
    const previous = workspaceIdsRef.current;
    if (!previous) {
      workspaceIdsRef.current = workspaceIdsKey;
      setMode("browse");
      return;
    }
    if (previous === workspaceIdsKey) return;
    const prevIds = new Set(previous.split("\0").filter(Boolean));
    const nextIds = new Set(workspaceIdsKey.split("\0").filter(Boolean));
    const overlap = [...nextIds].some((id) => prevIds.has(id));
    workspaceIdsRef.current = workspaceIdsKey;
    // Completely different workspace → back to the tree landing view.
    if (!overlap) {
      setMode("browse");
      setPreviewId(null);
      setOpenTabs([]);
      setQuery("");
    }
  }, [workspaceIdsKey, items.length]);

  useEffect(() => {
    // External selection (e.g. Artifacts panel) should open preview immediately.
    if (!selectedId) return;
    if (!items.some((item) => item.id === selectedId)) return;
    if (selectedId.startsWith("artifact:")) {
      setOpenTabs((current) => (current.includes(selectedId) ? current : [...current, selectedId]));
      setPreviewId(selectedId);
      setMode("preview");
    } else if (mode === "preview") {
      setPreviewId(selectedId);
    }
  }, [selectedId, items, mode]);

  const previewKind = selected
    ? detectPreviewKind(selected.title, selected.content)
    : "empty";

  function toggleDir(path: string) {
    setExpandedDirs((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function openFile(id: string) {
    setOpenTabs((current) => (current.includes(id) ? current : [...current, id]));
    setPreviewId(id);
    setMode("preview");
    onSelect(id);
  }

  function closeTab(id: string) {
    setOpenTabs((current) => {
      const next = current.filter((item) => item !== id);
      if (previewId === id) {
        const fallback = next[next.length - 1] ?? null;
        setPreviewId(fallback);
        setMode(fallback ? "preview" : "browse");
      }
      return next;
    });
  }

  const tree = (
    <div className="content-stage-tree-scroll" role="tree" aria-label="文件结构">
      {visibleTree.length === 0 ? (
        <p className="content-stage-tree-empty">没有匹配的文件</p>
      ) : visibleTree.map((node) => (
        <TreeNodeView
          key={node.path}
          node={node}
          depth={0}
          selectedId={previewId}
          interactive
          expandedDirs={expandedDirs}
          forceOpen={query.trim().length > 0}
          onToggleDir={toggleDir}
          onSelect={openFile}
        />
      ))}
    </div>
  );

  const fileSearch = (
    <label className="content-stage-file-search">
      <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.2" /><path d="M10.2 10.2 13.2 13.2" /></svg>
      <input
        value={query}
        placeholder="按文件名搜索..."
        aria-label="按文件名搜索"
        onChange={(event) => setQuery(event.target.value)}
      />
    </label>
  );

  const tabs = items.length > 0 ? (
    <div className="content-stage-tabs" role="tablist" aria-label="文件与预览">
      <button
        className={`content-stage-tab ${mode === "browse" ? "active" : ""}`}
        type="button"
        role="tab"
        aria-selected={mode === "browse"}
        onClick={() => setMode("browse")}
      >
        <FolderGlyph />
        <span>文件</span>
      </button>
      {openTabs.map((id) => {
        const item = items.find((entry) => entry.id === id);
        if (!item) return null;
        const active = mode === "preview" && selected?.id === id;
        return (
          <div className={`content-stage-tab ${active ? "active" : ""}`} key={id} role="tab" aria-selected={active}>
            <button type="button" onClick={() => openFile(id)}>
              <FileGlyph />
              <span>{item.title}</span>
            </button>
            <button type="button" aria-label={`关闭 ${item.title}`} onClick={() => closeTab(id)}>
              <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3.2 3.2 8.8 8.8M8.8 3.2 3.2 8.8" /></svg>
            </button>
          </div>
        );
      })}
    </div>
  ) : null;

  const body = (
    <div className={`content-stage-body-shell mode-${mode}`}>
      {items.length === 0 ? (
        <div className="content-stage-empty">
          <span>◇</span>
          <strong>{emptyTitle}</strong>
          <p>{emptyHint}</p>
        </div>
      ) : mode === "browse" ? (
        <div className="content-stage-browse">
          {fileSearch}
          {tree}
        </div>
      ) : selected ? (
        <section
          className={`content-stage-preview kind-${previewKind}`}
          aria-label={selected.title}
        >
          {selected.footnote ? (
            <p className="content-stage-footnote" title={selected.footnote}>
              {selected.footnote}
            </p>
          ) : null}
          <div className="content-stage-body">
            <FilePreview
              title={selected.title}
              path={selected.subtitle ?? selected.id}
              content={selected.content}
              items={items}
              resolveFile={resolveFile}
            />
          </div>
        </section>
      ) : (
        <div className="content-stage-browse">
          {fileSearch}
          {tree}
        </div>
      )}

      {footer ? <footer className="content-stage-footer">{footer}</footer> : null}
    </div>
  );

  const chrome = !embedded ? (
    <header className="content-stage-chrome">
      <div className="content-stage-brand">
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
      </div>
      <span className="content-stage-count">{items.length || ""}</span>
    </header>
  ) : null;

  if (embedded) {
    return (
      <div className={`content-stage content-stage-embedded mode-${mode}`}>
        {tabs}
        {body}
      </div>
    );
  }

  return (
    <aside className={`content-stage inspector mode-${mode}`} aria-label={title}>
      {chrome}
      {tabs}
      {body}
    </aside>
  );
}


type FileTreeNode = {
  name: string;
  path: string;
  kind: "dir" | "file";
  item?: ContentStageItem;
  children: FileTreeNode[];
};

function TreeNodeView({
  node,
  depth,
  selectedId,
  interactive,
  expandedDirs,
  forceOpen,
  onToggleDir,
  onSelect
}: {
  node: FileTreeNode;
  depth: number;
  selectedId: string | null;
  interactive: boolean;
  expandedDirs: Set<string>;
  forceOpen: boolean;
  onToggleDir: (path: string) => void;
  onSelect: (id: string) => void;
}) {
  const [tip, setTip] = useState<DOMRect | null>(null);
  const showTip = (event: MouseEvent<HTMLButtonElement>) => {
    const label = event.currentTarget.querySelector("strong");
    if (!label || label.scrollWidth <= label.clientWidth + 1) {
      setTip(null);
      return;
    }
    setTip(label.getBoundingClientRect());
  };

  if (node.kind === "dir") {
    const open = forceOpen || expandedDirs.has(node.path);
    return (
      <div className="content-stage-tree-dir" role="group" aria-label={node.name}>
        <button
          className={`content-stage-tree-row dir ${open ? "open" : ""}`}
          type="button"
          role="treeitem"
          aria-expanded={open}
          tabIndex={interactive ? 0 : -1}
          style={{ paddingLeft: 8 + depth * 16 }}
          onClick={() => onToggleDir(node.path)}
          onMouseEnter={showTip}
          onMouseLeave={() => setTip(null)}
        >
          <FolderGlyph open={open} />
          <span className="content-stage-item-copy">
            <strong>{node.name}</strong>
          </span>
        </button>
        {tip ? <NameTip rect={tip} text={node.name} /> : null}
        {open ? (
          <div className="content-stage-tree-children">
            {node.children.map((child) => (
              <TreeNodeView
                key={child.path}
                node={child}
                depth={depth + 1}
                selectedId={selectedId}
                interactive={interactive}
                expandedDirs={expandedDirs}
                forceOpen={forceOpen}
                onToggleDir={onToggleDir}
                onSelect={onSelect}
              />
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  const active = selectedId === node.item?.id;
  return (
    <button
      className={`content-stage-tree-row file ${active ? "active" : ""} ${node.item?.badgeTone ?? ""}`}
      type="button"
      role="treeitem"
      aria-selected={active}
      tabIndex={interactive ? 0 : -1}
      style={{ paddingLeft: 8 + depth * 16 }}
      onClick={() => {
        if (node.item) onSelect(node.item.id);
      }}
      onMouseEnter={showTip}
      onMouseLeave={() => setTip(null)}
    >
      <FileGlyph />
      <span className="content-stage-item-copy">
        <strong>{node.name}</strong>
      </span>
      {node.item?.badge ? <em>{node.item.badge}</em> : null}
      {tip ? <NameTip rect={tip} text={node.name} /> : null}
    </button>
  );
}

function FolderGlyph({ open = false }: { open?: boolean }) {
  return (
    <span className="content-stage-glyph" aria-hidden>
      {open ? (
        <svg viewBox="0 0 16 16">
          <path d="M1.6 6.5h12.8v6.2H1.6z" />
          <path d="M1.6 6.5 3.3 4h3.3l1.1 1.3H14.4" />
        </svg>
      ) : (
        <svg viewBox="0 0 16 16">
          <path d="M1.8 4.2h4.1l1.2 1.4h7.1v7.2H1.8z" />
        </svg>
      )}
    </span>
  );
}

function FileGlyph() {
  return (
    <span className="content-stage-glyph" aria-hidden>
      <svg viewBox="0 0 16 16">
        <path d="M4.2 1.8h4.8L12.2 4.8v9.4H4.2z" />
        <path d="M9 1.8v3.2h3.2" />
      </svg>
    </span>
  );
}

function NameTip({ rect, text }: { rect: DOMRect; text: string }) {
  return createPortal(
    <div className="content-stage-name-tip" style={{ left: rect.right - 6, top: rect.top + rect.height / 2 }}>{text}</div>,
    document.body
  );
}

function filterFileTree(nodes: FileTreeNode[], query: string): FileTreeNode[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return nodes;
  const walk = (entries: FileTreeNode[]): FileTreeNode[] => {
    const next: FileTreeNode[] = [];
    for (const node of entries) {
      if (node.kind === "file") {
        if (node.name.toLowerCase().includes(needle)) next.push(node);
        continue;
      }
      const children = walk(node.children);
      if (children.length || node.name.toLowerCase().includes(needle)) {
        next.push({ ...node, children });
      }
    }
    return next;
  };
  return walk(nodes);
}

function buildFileTree(items: ContentStageItem[]): FileTreeNode[] {
  const root: FileTreeNode[] = [];

  for (const item of items) {
    const path = normalizeWorkspacePath(item.subtitle || item.id || item.title);
    if (!path || path.startsWith("artifact:")) {
      root.push({
        name: item.title,
        path: item.id,
        kind: "file",
        item,
        children: []
      });
      continue;
    }

    const parts = path.split("/").filter(Boolean);
    let level = root;
    let prefix = "";
    parts.forEach((part, index) => {
      prefix = prefix ? `${prefix}/${part}` : part;
      const isFile = index === parts.length - 1;
      let node = level.find((entry) => entry.name === part && entry.kind === (isFile ? "file" : "dir"));
      if (!node) {
        node = {
          name: part,
          path: prefix,
          kind: isFile ? "file" : "dir",
          item: isFile ? item : undefined,
          children: []
        };
        level.push(node);
      } else if (isFile) {
        node.item = item;
      }
      if (!isFile) level = node.children;
    });
  }

  sortTree(root);
  return root;
}

function sortTree(nodes: FileTreeNode[]) {
  nodes.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  });
  for (const node of nodes) {
    if (node.children.length) sortTree(node.children);
  }
}

function collectDirPaths(nodes: FileTreeNode[]): Set<string> {
  const dirs = new Set<string>();
  const walk = (entries: FileTreeNode[]) => {
    for (const node of entries) {
      if (node.kind !== "dir") continue;
      dirs.add(node.path);
      if (node.children.length) walk(node.children);
    }
  };
  walk(nodes);
  return dirs;
}

function FilePreview({
  title,
  path,
  content,
  items,
  resolveFile
}: {
  title: string;
  path: string;
  content: string;
  items: ContentStageItem[];
  resolveFile?: (path: string) => Promise<string | null>;
}) {
  const kind = detectPreviewKind(title, content);

  if (kind === "empty") {
    return (
      <div className="content-stage-empty compact">
        <p>这份成果还没有可预览的正文</p>
      </div>
    );
  }

  if (kind === "binary") {
    return (
      <div className="content-stage-empty compact">
        <p>{content.trim() || "二进制文件暂不支持预览"}</p>
      </div>
    );
  }

  if (kind === "markdown") {
    return (
      <div className="content-stage-markdown">
        <MarkdownContent content={content} />
      </div>
    );
  }

  if (kind === "html") {
    return (
      <HtmlPreview
        htmlPath={path}
        html={content}
        items={items}
        resolveFile={resolveFile}
      />
    );
  }

  if (kind === "svg") {
    return (
      <div
        className="content-stage-svg-preview"
        dangerouslySetInnerHTML={{ __html: sanitizeSvg(content) }}
      />
    );
  }

  if (kind === "json") {
    return (
      <pre className="content-stage-code-preview">
        <code>{prettyJson(content)}</code>
      </pre>
    );
  }

  if (kind === "csv") {
    return <CsvPreview content={content} />;
  }

  if (kind === "code") {
    return (
      <pre className="content-stage-code-preview">
        <code>{content}</code>
      </pre>
    );
  }

  return (
    <pre className="content-stage-text-preview">
      <code>{content}</code>
    </pre>
  );
}

function HtmlPreview({
  htmlPath,
  html,
  items,
  resolveFile
}: {
  htmlPath: string;
  html: string;
  items: ContentStageItem[];
  resolveFile?: (path: string) => Promise<string | null>;
}) {
  const [srcDoc, setSrcDoc] = useState<string | null>(null);
  const [building, setBuilding] = useState(true);
  const itemsRef = useRef(items);
  const resolveFileRef = useRef(resolveFile);
  const shownPathRef = useRef(htmlPath);
  itemsRef.current = items;
  resolveFileRef.current = resolveFile;

  // Rebuild when the HTML changes or when linked CSS/JS bodies become available.
  const assetKey = items
    .filter((item) => /\.(css|js|mjs|cjs)$/i.test(item.id) && usableFileContent(item.content))
    .map((item) => `${item.id}:${item.content.length}:${item.content.slice(0, 48)}`)
    .sort()
    .join("|");

  useEffect(() => {
    let cancelled = false;
    const pathChanged = shownPathRef.current !== htmlPath;
    shownPathRef.current = htmlPath;
    if (pathChanged) {
      setSrcDoc(null);
      setBuilding(true);
    } else {
      setBuilding(true);
    }

    const cache = new Map<string, string>();
    for (const item of itemsRef.current) {
      if (usableFileContent(item.content)) {
        cache.set(normalizeWorkspacePath(item.id), stripTruncationMarker(item.content));
      }
    }

    async function load(path: string): Promise<string | null> {
      const key = normalizeWorkspacePath(path);
      const hit = cache.get(key);
      if (hit) return hit;
      const resolver = resolveFileRef.current;
      if (!resolver) return null;
      const loaded = await resolver(key);
      if (!loaded || !usableFileContent(loaded)) return null;
      const cleaned = stripTruncationMarker(loaded);
      cache.set(key, cleaned);
      return cleaned;
    }

    void (async () => {
      const documentHtml = await buildSelfContainedHtml(htmlPath, html, load);
      if (cancelled) return;
      setSrcDoc(documentHtml);
      setBuilding(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [htmlPath, html, assetKey]);

  return (
    <div className={`content-stage-html-frame ${building && !srcDoc ? "pending" : ""}`}>
      {srcDoc ? (
        <iframe
          className="content-stage-html-preview"
          title="HTML 预览"
          sandbox="allow-scripts allow-forms allow-popups allow-modals"
          srcDoc={srcDoc}
        />
      ) : (
        <div className="content-stage-empty compact">
          <p>正在组装页面预览…</p>
        </div>
      )}
    </div>
  );
}

async function buildSelfContainedHtml(
  htmlPath: string,
  html: string,
  load: (path: string) => Promise<string | null>
): Promise<string> {
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, "text/html");

  // Relative stylesheets / scripts have no origin inside srcDoc; inline them
  // from the workspace so the preview matches the published site.
  for (const link of Array.from(doc.querySelectorAll("link[href]"))) {
    const rel = (link.getAttribute("rel") || "").toLowerCase();
    const asAttr = (link.getAttribute("as") || "").toLowerCase();
    const type = (link.getAttribute("type") || "").toLowerCase();
    const href = link.getAttribute("href") || "";
    const isStylesheet =
      rel.split(/\s+/).includes("stylesheet")
      || type === "text/css"
      || (rel.includes("preload") && asAttr === "style");
    if (!isStylesheet) continue;
    const assetPath = resolveWorkspaceRelative(htmlPath, href);
    if (!assetPath) continue;
    const css = await load(assetPath);
    if (css == null) continue;
    const expanded = await expandCssImports(css, assetPath, load);
    const style = doc.createElement("style");
    style.setAttribute("data-preview-src", assetPath);
    style.textContent = expanded;
    link.replaceWith(style);
  }

  for (const script of Array.from(doc.querySelectorAll("script[src]"))) {
    const src = script.getAttribute("src") || "";
    const assetPath = resolveWorkspaceRelative(htmlPath, src);
    if (!assetPath) continue;
    const js = await load(assetPath);
    if (js == null) continue;
    const replacement = doc.createElement("script");
    for (const attr of Array.from(script.attributes)) {
      if (attr.name === "src") continue;
      replacement.setAttribute(attr.name, attr.value);
    }
    replacement.setAttribute("data-preview-src", assetPath);
    replacement.textContent = js;
    script.replaceWith(replacement);
  }

  const serialized = "<!DOCTYPE html>\n" + doc.documentElement.outerHTML;
  return serialized;
}

async function expandCssImports(
  css: string,
  cssPath: string,
  load: (path: string) => Promise<string | null>,
  depth = 0
): Promise<string> {
  if (depth > 4) return css;
  const importPattern = /@import\s+(?:url\(\s*)?(['"]?)([^'")\s]+)\1\s*\)?\s*;/gi;
  const parts: string[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = importPattern.exec(css)) !== null) {
    parts.push(css.slice(lastIndex, match.index));
    lastIndex = match.index + match[0].length;
    const href = match[2];
    const assetPath = resolveWorkspaceRelative(cssPath, href);
    if (!assetPath) {
      parts.push(match[0]);
      continue;
    }
    const imported = await load(assetPath);
    if (imported == null) {
      parts.push(match[0]);
      continue;
    }
    parts.push(await expandCssImports(imported, assetPath, load, depth + 1));
  }
  parts.push(css.slice(lastIndex));
  return parts.join("\n");
}

function resolveWorkspaceRelative(fromFile: string, href: string): string | null {
  const cleaned = href.trim().split(/[?#]/)[0] ?? "";
  if (!cleaned || cleaned.startsWith("data:") || /^[a-z][a-z0-9+.-]*:/i.test(cleaned)) {
    return null;
  }
  if (cleaned.startsWith("/")) {
    return normalizeWorkspacePath(cleaned.slice(1));
  }
  const fromDir = fromFile.includes("/")
    ? fromFile.slice(0, fromFile.lastIndexOf("/"))
    : "";
  const segments = [
    ...(fromDir ? fromDir.split("/") : []),
    ...cleaned.split("/")
  ];
  const stack: string[] = [];
  for (const segment of segments) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      if (!stack.length) return null;
      stack.pop();
      continue;
    }
    stack.push(segment);
  }
  return stack.join("/");
}

function normalizeWorkspacePath(path: string): string {
  return path.replace(/\\/g, "/").replace(/^\.\/+/, "").replace(/^\/+/, "");
}

function usableFileContent(content: string): boolean {
  const trimmed = content.trim();
  if (!trimmed) return false;
  if (PLACEHOLDER_CONTENT.has(trimmed)) return false;
  if (trimmed.startsWith("（二进制文件")) return false;
  return true;
}

function stripTruncationMarker(content: string): string {
  return content.replace(/\n\n…（内容已截断）\s*$/, "");
}

function detectPreviewKind(title: string, content: string): PreviewKind {
  const trimmed = content.trim();
  if (!trimmed) return "empty";
  if (trimmed.startsWith("（二进制文件") || trimmed === "读取失败") return "binary";

  const name = title.toLowerCase();
  const extension = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : "";

  if (["md", "markdown", "mdx"].includes(extension)) return "markdown";
  if (["html", "htm"].includes(extension) || /^<!doctype html|<html[\s>]/i.test(trimmed)) {
    return "html";
  }
  if (extension === "svg" || trimmed.startsWith("<svg")) return "svg";
  if (["json", "webmanifest"].includes(extension)) return "json";
  if (["csv", "tsv"].includes(extension)) return "csv";
  if ([
    "js", "jsx", "ts", "tsx", "mjs", "cjs", "css", "scss", "less",
    "py", "rb", "go", "rs", "java", "kt", "swift", "c", "h", "cpp", "hpp",
    "sh", "bash", "zsh", "sql", "yml", "yaml", "toml", "ini", "cfg",
    "xml", "env", "log", "txt"
  ].includes(extension)) {
    return "code";
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      JSON.parse(trimmed);
      return "json";
    } catch {
      // Fall through to plain text.
    }
  }
  if (/^#{1,6}\s|^\*\*|^\-\s|^\d+\.\s/m.test(trimmed)) return "markdown";
  return "text";
}

function prettyJson(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

function sanitizeSvg(content: string): string {
  return content
    .replace(/<script[\s\S]*?>[\s\S]*?<\/script>/gi, "")
    .replace(/\son[a-z]+\s*=\s*(['"]).*?\1/gi, "");
}

function CsvPreview({ content }: { content: string }) {
  const rows = parseDelimited(content);
  if (!rows.length) {
    return (
      <pre className="content-stage-text-preview">
        <code>{content}</code>
      </pre>
    );
  }
  const header = rows[0];
  const body = rows.slice(1, 201);
  return (
    <div className="content-stage-table-wrap">
      <table className="content-stage-table">
        <thead>
          <tr>
            {header.map((cell, index) => (
              <th key={`h-${index}`}>{cell || `列 ${index + 1}`}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            <tr key={`r-${rowIndex}`}>
              {header.map((_, cellIndex) => (
                <td key={`c-${rowIndex}-${cellIndex}`}>{row[cellIndex] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 201 ? <p className="content-stage-table-note">仅预览前 200 行</p> : null}
    </div>
  );
}

function parseDelimited(content: string): string[][] {
  const firstLine = content.split(/\r?\n/, 1)[0] ?? "";
  const delimiter = firstLine.includes("\t") ? "\t" : ",";
  return content
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line, index, lines) => line.length > 0 || index < lines.length - 1)
    .filter(Boolean)
    .map((line) => splitDelimitedLine(line, delimiter));
}

function splitDelimitedLine(line: string, delimiter: string): string[] {
  const cells: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === '"') {
      if (inQuotes && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }
    if (char === delimiter && !inQuotes) {
      cells.push(current);
      current = "";
      continue;
    }
    current += char;
  }
  cells.push(current);
  return cells;
}
