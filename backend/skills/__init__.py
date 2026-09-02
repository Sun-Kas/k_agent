"""Backend 的 Skill 正文懒加载边界；catalog 元数据始终来自请求。"""

from backend.skills.body import SkillBody, SkillBodyError, load_skill_body

__all__ = ["SkillBody", "SkillBodyError", "load_skill_body"]
