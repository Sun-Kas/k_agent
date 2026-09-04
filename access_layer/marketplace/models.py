"""广场内部 DTO。对外 JSON 只使用这些字段名。"""

from __future__ import annotations

from typing import Any


def listing(
    items: list[dict[str, Any]],
    *,
    source_status: str,
    warnings: list[str] | None = None,
    next_cursor: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    return {
        "items": items,
        "page": {
            "nextCursor": next_cursor,
            "page": page,
            "pageSize": page_size,
            "total": total,
        },
        "sourceStatus": source_status,
        "warnings": list(warnings or []),
    }


def marketplace_item(
    *,
    kind: str,
    source: str,
    source_id: str,
    title: str,
    summary: str,
    version: str | None = None,
    icon_url: str | None = None,
    icons: list[Any] | None = None,
    homepage: str | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    stats: dict[str, Any] | None = None,
    official_status: str | None = None,
    installed: bool = False,
    local_id: str | None = None,
    install_preview: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "source": source,
        "sourceId": source_id,
        "title": title,
        "summary": summary,
        "version": version,
        "iconUrl": icon_url,
        "icons": icons or [],
        "homepage": homepage,
        "categories": categories or [],
        "tags": tags or [],
        "stats": stats or {"downloads": None, "installs": None, "score": None},
        "officialStatus": official_status,
        "installed": installed,
        "localId": local_id,
        "installPreview": install_preview,
    }
    if extra:
        payload.update(extra)
    return payload
