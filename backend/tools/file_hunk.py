"""Match-anchored unified hunks for file-write receipts shown in the chat card."""

from __future__ import annotations

from typing import Any

HUNK_CONTEXT = 3
MAX_HUNK_LINES = 400
MAX_EDIT_HUNKS = 8
_LCS_CELL_LIMIT = 80_000


def hunks_for_edit(
    before: str,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
    context: int = HUNK_CONTEXT,
) -> list[dict[str, Any]]:
    """Anchor each hunk at find(old), then keep a few file lines around the change.

    This follows the Claude Code / StrReplace preview: line numbers come from the
    match offset in the current file, not from a whole-file LCS.
    """

    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    before_lines = _file_lines(before)
    after_lines = _file_lines(after)
    hunks: list[dict[str, Any]] = []
    search_at = 0
    occurrence = 0
    while old:
        index = before.find(old, search_at)
        if index < 0:
            break
        new_index = index + occurrence * (len(new) - len(old))
        hunk = _hunk_at_match(
            before_lines,
            after_lines,
            before,
            after,
            index,
            new_index,
            old,
            new,
            context,
        )
        if hunk is not None:
            hunks.append(hunk)
        occurrence += 1
        search_at = index + len(old)
        if not replace_all or occurrence >= MAX_EDIT_HUNKS:
            break
    return hunks


def unified_hunks(before: str, after: str, *, context: int = HUNK_CONTEXT) -> list[dict[str, Any]]:
    """Whole-file hunks for Write, where there is no replacement anchor."""

    tagged = _numbered_diff(_file_lines(before), _file_lines(after))
    change_indexes = [index for index, line in enumerate(tagged) if line["kind"] != "ctx"]
    if not change_indexes:
        return []
    groups: list[tuple[int, int]] = []
    start = previous_change = change_indexes[0]
    for index in change_indexes[1:]:
        if index <= previous_change + (context * 2) + 1:
            previous_change = index
            continue
        groups.append((start, previous_change))
        start = previous_change = index
    groups.append((start, previous_change))
    hunks: list[dict[str, Any]] = []
    rendered = 0
    for lo, hi in groups:
        from_index = max(0, lo - context)
        to_index = min(len(tagged), hi + context + 1)
        packed = _pack_hunk(tagged[from_index:to_index])
        if packed is None:
            continue
        rendered += len(packed["lines"])
        hunks.append(packed)
        if rendered >= MAX_HUNK_LINES:
            break
    return hunks


def _hunk_at_match(
    before_lines: list[str],
    after_lines: list[str],
    before: str,
    after: str,
    index: int,
    new_index: int,
    old: str,
    new: str,
    context: int,
) -> dict[str, Any] | None:
    old_start = before.count("\n", 0, index)
    old_end = before.count("\n", 0, index + max(len(old) - 1, 0))
    new_start = after.count("\n", 0, new_index)
    new_end = after.count("\n", 0, new_index + max(len(new) - 1, 0)) if new else new_start - 1
    old_body = before_lines[old_start : old_end + 1]
    new_body = after_lines[new_start : new_end + 1] if new else []
    prefix_from = max(0, old_start - context)
    prefix = before_lines[prefix_from:old_start]
    new_prefix_from = max(0, new_start - len(prefix))
    suffix_from = old_end + 1
    suffix = before_lines[suffix_from : suffix_from + context]
    new_suffix_from = new_end + 1 if new else new_start

    tagged: list[dict[str, Any]] = []
    for offset, text in enumerate(prefix):
        tagged.append(
            {
                "kind": "ctx",
                "text": text,
                "oldLine": prefix_from + offset + 1,
                "newLine": new_prefix_from + offset + 1,
            }
        )
    for line in _numbered_diff(old_body, new_body):
        if "oldLine" in line:
            line["oldLine"] = old_start + int(line["oldLine"])
        if "newLine" in line:
            line["newLine"] = new_start + int(line["newLine"])
        tagged.append(line)
    for offset, text in enumerate(suffix):
        tagged.append(
            {
                "kind": "ctx",
                "text": text,
                "oldLine": suffix_from + offset + 1,
                "newLine": new_suffix_from + offset + 1,
            }
        )
    return _pack_hunk(_collapse(tagged, context))


def _collapse(tagged: list[dict[str, Any]], context: int) -> list[dict[str, Any]]:
    change_indexes = [index for index, line in enumerate(tagged) if line["kind"] != "ctx"]
    if not change_indexes:
        return []
    keep: set[int] = set()
    last = len(tagged) - 1
    for index in change_indexes:
        keep.update(range(max(0, index - context), min(last, index + context) + 1))
    return [tagged[index] for index in sorted(keep)]


def _pack_hunk(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not lines:
        return None
    truncated = len(lines) > MAX_HUNK_LINES
    if truncated:
        lines = lines[:MAX_HUNK_LINES]
    old_rows = [line for line in lines if line["kind"] != "add"]
    new_rows = [line for line in lines if line["kind"] != "del"]
    hunk: dict[str, Any] = {
        "oldStart": next((line["oldLine"] for line in lines if line.get("oldLine")), 1),
        "oldCount": len(old_rows),
        "newStart": next((line["newLine"] for line in lines if line.get("newLine")), 1),
        "newCount": len(new_rows),
        "lines": lines,
    }
    if truncated:
        hunk["truncated"] = True
    return hunk


def _file_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _numbered_diff(previous: list[str], next_lines: list[str]) -> list[dict[str, Any]]:
    if not previous and not next_lines:
        return []
    if not previous:
        return [
            {"kind": "add", "text": text, "newLine": index}
            for index, text in enumerate(next_lines, start=1)
        ]
    if not next_lines:
        return [
            {"kind": "del", "text": text, "oldLine": index}
            for index, text in enumerate(previous, start=1)
        ]
    if len(previous) * len(next_lines) > _LCS_CELL_LIMIT:
        return [
            *[
                {"kind": "del", "text": text, "oldLine": index}
                for index, text in enumerate(previous, start=1)
            ],
            *[
                {"kind": "add", "text": text, "newLine": index}
                for index, text in enumerate(next_lines, start=1)
            ],
        ]
    return _lcs_numbered(previous, next_lines)


def _lcs_numbered(previous: list[str], next_lines: list[str]) -> list[dict[str, Any]]:
    rows = len(previous)
    cols = len(next_lines)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            table[i][j] = (
                table[i - 1][j - 1] + 1
                if previous[i - 1] == next_lines[j - 1]
                else max(table[i - 1][j], table[i][j - 1])
            )
    reversed_lines: list[dict[str, Any]] = []
    i = rows
    j = cols
    while i > 0 or j > 0:
        if i > 0 and j > 0 and previous[i - 1] == next_lines[j - 1]:
            reversed_lines.append(
                {"kind": "ctx", "text": previous[i - 1], "oldLine": i, "newLine": j}
            )
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or table[i][j - 1] >= table[i - 1][j]):
            reversed_lines.append({"kind": "add", "text": next_lines[j - 1], "newLine": j})
            j -= 1
        else:
            reversed_lines.append({"kind": "del", "text": previous[i - 1], "oldLine": i})
            i -= 1
    reversed_lines.reverse()
    return reversed_lines
