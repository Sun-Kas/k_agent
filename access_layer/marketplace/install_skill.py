"""从 SkillHub 下载 zip 并交给现有 Skill 安装器。"""

from __future__ import annotations

from pathlib import Path

from access_layer.skills.archive import validate_and_install_skill_zip
from access_layer.marketplace.skillhub import SkillHubClient


async def download_and_install(
    client: SkillHubClient,
    slug: str,
    *,
    skills_root: Path,
    skill_id_override: str | None = None,
) -> tuple[str, str, Path]:
    archive = await client.download_zip(slug)
    return validate_and_install_skill_zip(
        archive,
        f"{slug}.zip",
        skills_root=skills_root,
        skill_id_override=skill_id_override,
    )
