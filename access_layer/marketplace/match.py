"""对照本机 catalog 判定广场条目是否已安装。"""

from __future__ import annotations

from typing import Any

from access_layer.skills.archive import normalize_imported_skill_id


def last_segment(source_id: str) -> str:
    return source_id.rstrip("/").rsplit("/", 1)[-1]


def match_installed(
    rows: list[dict[str, Any]],
    *,
    source: str,
    source_id: str,
) -> dict[str, Any] | None:
    """强匹配 marketplace.sourceId；否则弱匹配本地 id 与 name 末段。"""

    for row in rows:
        marketplace = row.get("marketplace") if isinstance(row.get("marketplace"), dict) else {}
        if marketplace.get("source") == source and str(marketplace.get("sourceId") or "") == source_id:
            return row
    weak_id = normalize_imported_skill_id(last_segment(source_id))
    for row in rows:
        if str(row.get("id") or "") == weak_id:
            return row
        if str(row.get("id") or "") == source_id:
            return row
    return None
