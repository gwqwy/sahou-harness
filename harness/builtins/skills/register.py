"""技能插件：声明 profile 的 skills/ 目录（SKILL.md，渐进式披露由 chat_loop 接入）。"""

from __future__ import annotations

from pathlib import Path


def register(ctx) -> None:
    host = ctx.host
    profile_skills = Path(host.profile.root) / "skills"
    # 目录尚不存在时也声明——用户放入 SKILL.md 后重启即生效
    ctx.skills.add_dir(str(profile_skills))
