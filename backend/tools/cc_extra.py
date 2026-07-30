"""Supplementary Claude-Code-like tools for web, notebooks, and MCP resources."""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from backend.config import get_or_init_settings
from backend.tools.cc_like import _resolve_workspace_path, _tool_limits
from backend.tools.local import ToolDefinition


def _json(payload: dict[str, Any]) -> str:
    """把工具输出对象序列化为紧凑 JSON 字符串。"""
    return json.dumps(payload, ensure_ascii=False)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """按最大字符数截断工具输出。"""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n\n[truncated: kept first {max_chars} chars]", True


async def cc_ls(payload: dict[str, Any]) -> str:
    """列出目录内容，路径解析复用文件工具的工作区隔离逻辑。"""
    path = await _resolve_workspace_path(str(payload.get("path") or "."))
    if not path.exists():
        return _json({"ok": False, "error": "path not found", "path": str(path)})
    if not path.is_dir():
        return _json({"ok": False, "error": "path is not a directory", "path": str(path)})

    max_entries = int(payload.get("max_entries") or payload.get("maxEntries") or 200)
    show_hidden = bool(payload.get("show_hidden") or payload.get("showHidden") or False)
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if not show_hidden and child.name.startswith("."):
            continue
        stat = child.stat()
        entries.append({
            "name": child.name,
            "path": str(child),
            "type": "directory" if child.is_dir() else "file",
            "size": stat.st_size,
        })
        if len(entries) >= max_entries:
            break
    return _json({"ok": True, "path": str(path), "entries": entries, "truncated": len(entries) >= max_entries})


def _fetch_url_sync(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """同步 HTTP 抓取函数；外层会放到线程中运行，避免阻塞协程事件循环。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "K-Agent/1.0",
            **headers,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read()
        content_type = response.headers.get("content-type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        return {
            "status": response.status,
            "url": response.geturl(),
            "contentType": content_type,
            "text": text,
        }


async def cc_web_fetch(payload: dict[str, Any]) -> str:
    """抓取网页并转成文本，作为 Claude Code WebFetch 的轻量本地实现。"""
    url = str(payload.get("url") or "").strip()
    if not url:
        return _json({"ok": False, "error": "url is required"})
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _json({"ok": False, "error": "only http and https URLs are supported"})
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    settings = await get_or_init_settings()
    _, default_max_chars = await _tool_limits()
    max_chars = int(payload.get("max_chars") or payload.get("maxChars") or default_max_chars)
    try:
        # 网络请求放到线程里执行，避免阻塞当前请求所在的事件循环。
        result = await asyncio.to_thread(_fetch_url_sync, url, {str(k): str(v) for k, v in headers.items()}, settings.local_tool_bash_timeout_seconds)
    except Exception as exc:
        return _json({"ok": False, "error": str(exc), "url": url})
    text, truncated = _truncate(_html_to_text(result["text"]), max_chars)
    return _json({**result, "ok": True, "text": text, "truncated": truncated})


async def cc_web_search(payload: dict[str, Any]) -> str:
    """通过无脚本搜索页做轻量搜索；生产环境可替换为正式搜索 API。"""
    query = str(payload.get("query") or "").strip()
    if not query:
        return _json({"ok": False, "error": "query is required"})
    max_results = int(payload.get("max_results") or payload.get("maxResults") or 5)
    search_url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    fetched = json.loads(await cc_web_fetch({"url": search_url, "max_chars": 80000}))
    if not fetched.get("ok"):
        return _json(fetched)
    results = _parse_duckduckgo_results(str(fetched.get("text") or ""), max_results)
    return _json({"ok": True, "query": query, "results": results, "source": search_url})


async def cc_notebook_edit(payload: dict[str, Any]) -> str:
    """编辑 Jupyter notebook 单元格，只处理 ipynb JSON，不执行其中代码。"""
    path = await _resolve_workspace_path(str(payload.get("file_path") or payload.get("path") or ""))
    cell_index = int(payload.get("cell_index") if payload.get("cell_index") is not None else payload.get("cellIndex", -1))
    source = payload.get("source")
    cell_type = str(payload.get("cell_type") or payload.get("cellType") or "code")
    edit_mode = str(payload.get("mode") or "replace")
    if cell_index < 0:
        return _json({"ok": False, "error": "cell_index is required"})
    if source is None and edit_mode != "delete":
        return _json({"ok": False, "error": "source is required"})
    if path.suffix != ".ipynb":
        return _json({"ok": False, "error": "NotebookEdit only supports .ipynb files"})

    notebook = json.loads(path.read_text(encoding="utf-8"))
    cells = notebook.setdefault("cells", [])
    if edit_mode == "insert":
        if cell_index > len(cells):
            return _json({"ok": False, "error": "cell_index is out of range"})
        cells.insert(cell_index, _notebook_cell(cell_type, str(source)))
    elif edit_mode == "delete":
        if cell_index >= len(cells):
            return _json({"ok": False, "error": "cell_index is out of range"})
        del cells[cell_index]
    else:
        if cell_index >= len(cells):
            return _json({"ok": False, "error": "cell_index is out of range"})
        cells[cell_index] = {**cells[cell_index], **_notebook_cell(cell_type, str(source))}
    # ipynb 是 JSON 文档，保持缩进可以减少后续人工 diff 的阅读成本。
    path.write_text(json.dumps(notebook, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _json({"ok": True, "path": str(path), "cellIndex": cell_index, "mode": edit_mode})


def _notebook_cell(cell_type: str, source: str) -> dict[str, Any]:
    """按 Jupyter notebook 规范生成最小单元格结构。"""
    lines = source.splitlines(keepends=True)
    normalized = lines if lines else [""]
    if cell_type == "markdown":
        return {"cell_type": "markdown", "metadata": {}, "source": normalized}
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": normalized}


def _html_to_text(raw: str) -> str:
    """把 HTML 粗略清洗成模型可消费文本，避免把脚本和样式塞进上下文。"""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \\t\\r\\f\\v]+", " ", text).strip()


def _parse_duckduckgo_results(text: str, max_results: int) -> list[dict[str, str]]:
    """从 DuckDuckGo HTML 文本中提取搜索结果。"""
    results: list[dict[str, str]] = []
    # DuckDuckGo 的无脚本页面会把标题、摘要与 URL 渲染成连续文本；这里做保守提取，失败时返回空结果而不是编造。
    for match in re.finditer(r"(https?://[^\\s]+)", text):
        url = match.group(1).rstrip(").,;")
        if "duckduckgo.com" in urllib.parse.urlparse(url).netloc:
            continue
        if any(item["url"] == url for item in results):
            continue
        start = max(0, match.start() - 160)
        title = text[start:match.start()].strip().split("  ")[-1][-120:].strip()
        results.append({"title": title or url, "url": url, "snippet": ""})
        if len(results) >= max_results:
            break
    return results


def build_mcp_resource_tools(
    list_resources: Callable[[], Awaitable[dict[str, list[dict[str, Any]]]]] | None = None,
    read_resource: Callable[[str, str], Awaitable[str]] | None = None,
) -> list[ToolDefinition]:
    """构造 MCP 资源工具；实际执行函数会在请求级 manager 绑定后注入。"""

    async def execute_list(_: dict[str, Any]) -> str:
        """列出 MCP resources 或 prompts。"""
        if list_resources is None:
            return _json({"ok": False, "error": "MCP manager is not bound"})
        return _json({"ok": True, "resources": await list_resources()})

    async def execute_read(payload: dict[str, Any]) -> str:
        """读取指定 MCP resource 内容。"""
        if read_resource is None:
            return _json({"ok": False, "error": "MCP manager is not bound"})
        server_id = str(payload.get("server_id") or payload.get("serverId") or "").strip()
        uri = str(payload.get("uri") or "").strip()
        if not server_id or not uri:
            return _json({"ok": False, "error": "server_id and uri are required"})
        return await read_resource(server_id, uri)

    return [
        # 这两个工具对应 Claude Code 的 MCP resource 工具名，便于后续权限规则和提示词复用同一套名称。
        ToolDefinition(
            name="ListMcpResourcesTool",
            description="List resources exposed by connected MCP servers.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=execute_list,
        ),
        ToolDefinition(
            name="ReadMcpResourceTool",
            description="Read a resource from a connected MCP server by server_id and uri.",
            parameters={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "uri": {"type": "string"},
                },
                "required": ["server_id", "uri"],
                "additionalProperties": False,
            },
            execute=execute_read,
        ),
    ]


CC_EXTRA_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="LS",
        description="List files and directories under a workspace directory.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 200},
                "show_hidden": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        execute=cc_ls,
    ),
    ToolDefinition(
        name="WebFetch",
        description="Fetch a web page over HTTP or HTTPS and return readable text.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "headers": {"type": "object"},
                "max_chars": {"type": "integer", "default": 12000},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        execute=cc_web_fetch,
    ),
    ToolDefinition(
        name="WebSearch",
        description="Search the web and return a small list of matching pages.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        execute=cc_web_search,
    ),
    ToolDefinition(
        name="NotebookEdit",
        description="Insert, replace, or delete a cell in a Jupyter .ipynb notebook.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "cell_index": {"type": "integer"},
                "source": {"type": "string"},
                "cell_type": {"type": "string", "default": "code"},
                "mode": {"type": "string", "default": "replace"},
            },
            "required": ["file_path", "cell_index"],
            "additionalProperties": False,
        },
        execute=cc_notebook_edit,
    ),
]
