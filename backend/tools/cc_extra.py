"""Supplementary Claude-Code-like tools for web, notebooks, and MCP resources."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from backend.config import get_or_init_settings
from backend.tools.cc_like import _requests_host_access, _resolve_workspace_path, _tool_limits
from backend.tools.local import ToolDefinition
from backend.tools.workspace import current_tool_network_access


def _json(payload: dict[str, Any]) -> str:
    """把工具输出对象序列化为紧凑 JSON 字符串。"""
    return json.dumps(payload, ensure_ascii=False)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """按最大字符数截断工具输出。"""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n\n[truncated: kept first {max_chars} chars]", True


async def cc_ls(payload: dict[str, Any]) -> str:
    """列出任意本机目录内容；只读目录遍历无需提升写权限。"""
    path = await _resolve_workspace_path(str(payload.get("path") or "."), allow_outside=True)
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


def _allow_local_fetch() -> bool:
    """Escape hatch for deliberately fetching a service on this machine."""

    return (os.getenv("K_AGENT_ALLOW_LOCAL_WEB_FETCH") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_public_address(host: str) -> bool:
    """Whether every address this host resolves to is outside the local network.

    WebFetch takes a model-supplied URL, so without this check the tool is a
    ready-made SSRF probe into loopback services, container metadata endpoints,
    and the LAN the agent happens to run on.
    """

    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return bool(resolved)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect hop so a public URL cannot bounce inward."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise urllib.error.URLError(f"refused redirect to {newurl}")
        if not _is_public_address(parsed.hostname):
            raise urllib.error.URLError(
                f"refused redirect to non-public address {parsed.hostname}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_url_sync(
    url: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> dict[str, Any]:
    """同步 HTTP 抓取函数；外层会放到线程中运行，避免阻塞协程事件循环。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "K-Agent/1.0",
            **headers,
        },
    )
    opener = urllib.request.build_opener(_GuardedRedirectHandler)
    with opener.open(request, timeout=timeout) as response:  # noqa: S310
        # The response is truncated at the socket rather than after reading it,
        # so a large or endless body cannot exhaust memory before the character
        # limit is applied.
        raw = response.read(max_bytes + 1)
        oversized = len(raw) > max_bytes
        raw = raw[:max_bytes]
        content_type = response.headers.get("content-type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        return {
            "status": response.status,
            "url": response.geturl(),
            "contentType": content_type,
            "text": text,
            "downloadTruncated": oversized,
        }


async def cc_web_fetch(payload: dict[str, Any]) -> str:
    """抓取网页并转成文本，作为 Claude Code WebFetch 的轻量本地实现。"""
    if current_tool_network_access() is False:
        # Return a normal tool result so the model can revise its approach
        # instead of turning a policy denial into a terminal Agent run.
        return _json({"ok": False, "error": "network access is disabled for this run"})
    url = str(payload.get("url") or "").strip()
    if not url:
        return _json({"ok": False, "error": "url is required"})
    parsed = urllib.parse.urlparse(url)
    # 只放行 http/https：urllib 默认还支持 file:// 和 ftp://，
    # 前者会让这个工具变成绕过工作区限制的任意文件读取通道。
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _json({"ok": False, "error": "only http and https URLs are supported"})
    if not _allow_local_fetch() and not await asyncio.to_thread(
        _is_public_address, parsed.hostname or ""
    ):
        return _json({
            "ok": False,
            "error": (
                "refusing to fetch a loopback, private, or link-local address; "
                "set K_AGENT_ALLOW_LOCAL_WEB_FETCH=1 to override"
            ),
            "url": url,
        })
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    settings = await get_or_init_settings()
    _, default_max_chars = await _tool_limits()
    max_chars = int(payload.get("max_chars") or payload.get("maxChars") or default_max_chars)
    try:
        # 网络请求放到线程里执行，避免阻塞当前请求所在的事件循环。
        result = await asyncio.to_thread(
            _fetch_url_sync,
            url,
            {str(k): str(v) for k, v in headers.items()},
            settings.local_tool_bash_timeout_seconds,
            # UTF-8 text needs up to four bytes per character, so this keeps the
            # download bounded without truncating below the requested char cap.
            max_chars * 4,
        )
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
    path = await _resolve_workspace_path(
        str(payload.get("file_path") or payload.get("path") or ""),
        allow_outside=_requests_host_access(payload),
    )
    cell_index = int(payload.get("cell_index") if payload.get("cell_index") is not None else payload.get("cellIndex", -1))
    source = payload.get("source")
    cell_type = str(payload.get("cell_type") or payload.get("cellType") or "code")
    edit_mode = str(payload.get("mode") or "replace")
    if cell_index < 0:
        return _json({"ok": False, "error": "cell_index is required"})
    if source is None and edit_mode != "delete":
        return _json({"ok": False, "error": "source is required"})
    # 限定后缀，避免这个专用编辑器被当成通用写文件工具绕过 Write 的权限规则。
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
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _parse_duckduckgo_results(text: str, max_results: int) -> list[dict[str, str]]:
    """从 DuckDuckGo HTML 文本中提取搜索结果。"""
    results: list[dict[str, str]] = []
    # DuckDuckGo 的无脚本页面会把标题、摘要与 URL 渲染成连续文本；这里做保守提取，失败时返回空结果而不是编造。
    for match in re.finditer(r"(https?://[^\s]+)", text):
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
        # 未绑定 manager 说明工具是从模块级注册表直接取的（例如统计工具数量），
        # 返回结构化错误而不是抛异常，模型看到后会转而使用其他手段。
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
        description="List files and directories under any local directory. Read-only access does not require permission escalation.",
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
        context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
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
        context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
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
        context_policy={"mode": "rerunnable", "maxResultChars": 30_000},
    ),
    ToolDefinition(
        name="NotebookEdit",
        description="Insert, replace, or delete a cell in a Jupyter .ipynb notebook. Editing outside the workspace requires sandbox_permissions=require_escalated.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "cell_index": {"type": "integer"},
                "source": {"type": "string"},
                "cell_type": {"type": "string", "default": "code"},
                "mode": {"type": "string", "default": "replace"},
                "sandbox_permissions": {"type": "string", "enum": ["require_escalated"]},
            },
            "required": ["file_path", "cell_index"],
            "additionalProperties": False,
        },
        execute=cc_notebook_edit,
        context_policy={"mode": "receipt", "maxResultChars": 12_000},
    ),
]
