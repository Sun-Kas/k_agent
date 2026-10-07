"""web 领域工具；类型化业务结果在此产生。"""

from __future__ import annotations
from backend.tools.contracts import ToolExecutionContext
import asyncio
import json
from typing import Any
from backend.config import get_or_init_settings
from backend.tools.contracts import ToolExecutionPolicy
from backend.tools.factory import define_tool
from backend.tools.contracts import ToolOutcome
import html
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from backend.tools.builtins.common import _tool_limits, _json, _truncate


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


async def cc_web_fetch(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
    """抓取网页并转成文本，作为 Claude Code WebFetch 的轻量本地实现。"""
    if ctx.network_access is False:
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
        return _json(
            {
                "ok": False,
                "error": (
                    "refusing to fetch a loopback, private, or link-local address; "
                    "set K_AGENT_ALLOW_LOCAL_WEB_FETCH=1 to override"
                ),
                "url": url,
            }
        )
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    settings = await get_or_init_settings()
    _, default_max_chars = await _tool_limits(ctx)
    max_chars = int(
        payload.get("max_chars") or payload.get("maxChars") or default_max_chars
    )
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


async def cc_web_search(
    ctx: ToolExecutionContext, payload: dict[str, Any]
) -> ToolOutcome:
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
        title = text[start : match.start()].strip().split("  ")[-1][-120:].strip()
        results.append({"title": title or url, "url": url, "snippet": ""})
        if len(results) >= max_results:
            break
    return results


WEBFETCH_TOOL = define_tool(
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
    execution_policy=ToolExecutionPolicy("external", supports_live_output=False),
    permission_subjects=lambda args: (str(args.get("url") or "WebFetch"),),
)

WEBSEARCH_TOOL = define_tool(
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
    execution_policy=ToolExecutionPolicy("external", supports_live_output=False),
    permission_subjects=lambda args: (str(args.get("query") or "WebSearch"),),
)

WEB_TOOL_FACTORIES = (
    WEBFETCH_TOOL,
    WEBSEARCH_TOOL,
)
