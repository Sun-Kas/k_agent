from __future__ import annotations

import fnmatch
from pathlib import Path


def matches_rule(target_path: Path, globs: tuple[str, ...] | list[str], rule_dir: Path) -> bool:
    if not globs:
        return True
    target = str(target_path.resolve())
    candidates = [target, target_path.name]
    for base in (rule_dir, rule_dir.parent, rule_dir.parent.parent):
        try:
            candidates.append(str(target_path.resolve().relative_to(base.resolve())))
        except ValueError:
            continue
    return any(fnmatch.fnmatch(candidate, glob) for glob in globs for candidate in candidates)

