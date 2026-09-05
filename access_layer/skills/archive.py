"""Validate and atomically install a Skill zip into content/skills."""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from fastapi import HTTPException

from access_layer.skills.frontmatter import parse_markdown_frontmatter


MAX_SKILL_ZIP_BYTES = 20 * 1024 * 1024
MAX_SKILL_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_SKILL_ZIP_FILES = 500


def is_relative_to(path: Path, parent: Path) -> bool:
    """判断一个路径是否位于另一个路径之下。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def normalize_imported_skill_id(name: str) -> str:
    """把导入的 Skill 名称规范化为 content/skills 下的目录 ID。"""
    normalized = "".join(
        char.lower()
        if char.isascii() and (char.isalnum() or char == "_")
        else "-"
        for char in name
    )
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized[:80] or "skill"


def validate_and_install_skill_zip(
    archive: bytes,
    filename: str,
    *,
    skills_root: Path,
    skill_id_override: str | None = None,
) -> tuple[str, str, Path]:
    """校验 zip 后原子安装到 skills_root。目标已存在则 409。"""

    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 格式的 Skill 压缩包")
    if not archive:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(archive) > MAX_SKILL_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="压缩包超过 20MB 限制")

    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
            entries = _validated_skill_zip_entries(zip_file)
            skill_md = _find_skill_entry(entries)
            raw_skill = zip_file.read(skill_md).decode("utf-8", errors="replace")
            frontmatter, _ = parse_markdown_frontmatter(raw_skill)
            skill_name = str(frontmatter.get("name") or "").strip()
            description = str(frontmatter.get("description") or "").strip()
            if not skill_name:
                raise HTTPException(status_code=400, detail="SKILL.md frontmatter 必须包含 name")
            if not description:
                raise HTTPException(status_code=400, detail="SKILL.md frontmatter 必须包含 description")

            skill_root = _skill_archive_root(skill_md)
            _validate_single_skill_root(entries, skill_root)
            skill_id = normalize_imported_skill_id(skill_id_override or skill_name)
            if skill_id_override and skill_id != normalize_imported_skill_id(skill_id_override):
                raise HTTPException(status_code=400, detail="Skill ID 只能包含小写字母、数字、连字符或下划线")
            destination = skills_root / skill_id
            if destination.exists():
                raise HTTPException(status_code=409, detail=f'Skill "{skill_id}" 已存在')

            with tempfile.TemporaryDirectory(prefix="k-agent-skill-") as tmp:
                staging = Path(tmp) / skill_id
                staging.mkdir(parents=True)
                for entry in entries:
                    if entry.is_dir():
                        continue
                    relative = _relative_skill_member(entry.filename, skill_root)
                    if relative is None:
                        continue
                    target = (staging / relative).resolve()
                    if not is_relative_to(target, staging):
                        raise HTTPException(status_code=400, detail=f"压缩包包含非法路径：{entry.filename}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zip_file.open(entry) as source, target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                if not (staging / "SKILL.md").is_file():
                    raise HTTPException(status_code=400, detail="压缩包根目录必须包含 SKILL.md")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staging), str(destination))
            return skill_id, skill_name, destination
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="压缩包无法读取，请确认文件是有效 zip") from exc


def _validated_skill_zip_entries(zip_file: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = [entry for entry in zip_file.infolist() if not _is_ignored_zip_entry(entry.filename)]
    files = [entry for entry in entries if not entry.is_dir()]
    if not files:
        raise HTTPException(status_code=400, detail="压缩包中没有可导入的文件")
    if len(files) > MAX_SKILL_ZIP_FILES:
        raise HTTPException(status_code=413, detail=f"压缩包文件数量超过 {MAX_SKILL_ZIP_FILES} 个")
    total_size = 0
    for entry in files:
        _validate_zip_member(entry)
        total_size += entry.file_size
        if total_size > MAX_SKILL_UNPACKED_BYTES:
            raise HTTPException(status_code=413, detail="压缩包解压后超过 50MB 限制")
    return entries


def _validate_zip_member(entry: zipfile.ZipInfo) -> None:
    path = Path(entry.filename)
    parts = path.parts
    if path.is_absolute() or ".." in parts:
        raise HTTPException(status_code=400, detail=f"压缩包包含非法路径：{entry.filename}")
    if entry.file_size < 0 or entry.compress_size < 0:
        raise HTTPException(status_code=400, detail=f"压缩包条目异常：{entry.filename}")
    mode = (entry.external_attr >> 16) & 0o170000
    if mode in {stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK}:
        raise HTTPException(status_code=400, detail=f"压缩包不能包含链接或特殊文件：{entry.filename}")


def _find_skill_entry(entries: list[zipfile.ZipInfo]) -> str:
    skill_files = [
        entry.filename for entry in entries if not entry.is_dir() and Path(entry.filename).name == "SKILL.md"
    ]
    if not skill_files:
        raise HTTPException(status_code=400, detail="压缩包必须包含 SKILL.md")
    roots = {_skill_archive_root(path) for path in skill_files}
    if len(skill_files) > 1 or len(roots) > 1:
        raise HTTPException(status_code=400, detail="一个压缩包只能包含一个 Skill")
    return skill_files[0]


def _validate_single_skill_root(entries: list[zipfile.ZipInfo], root: tuple[str, ...]) -> None:
    for entry in entries:
        if entry.is_dir():
            continue
        if _relative_skill_member(entry.filename, root) is None:
            raise HTTPException(status_code=400, detail=f"压缩包包含 Skill 目录外的文件：{entry.filename}")


def _skill_archive_root(skill_path: str) -> tuple[str, ...]:
    parts = Path(skill_path).parts
    return tuple(parts[:-1])


def _relative_skill_member(filename: str, root: tuple[str, ...]) -> Path | None:
    parts = Path(filename).parts
    if root:
        if tuple(parts[: len(root)]) != root:
            return None
        parts = parts[len(root) :]
    if not parts:
        return None
    return Path(*parts)


def _is_ignored_zip_entry(filename: str) -> bool:
    parts = Path(filename).parts
    return not parts or parts[0] == "__MACOSX" or any(part == ".DS_Store" for part in parts)
