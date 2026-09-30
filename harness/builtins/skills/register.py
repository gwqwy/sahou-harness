"""技能插件：声明 profile 的 skills/ 目录（SKILL.md，渐进式披露由 chat_loop 接入）。

发行 exe 通过 PyInstaller --add-data 内置一份技能池（bundled_skills）；
在陌生环境首次启动时把 profile 里缺失的技能播种进去，之后一律以 profile
为准——内置池永不覆盖用户已安装/已修改的同名技能。开发态没有内置池，
本模块退化为纯粹的 profile 目录声明。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

BUNDLED_DIR_NAME = "bundled_skills"
SKILL_FILE = "SKILL.md"


def bundled_skills_root() -> Path | None:
    """冻结运行时的内置技能池目录；开发态（无 _MEIPASS）返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    root = Path(base) / BUNDLED_DIR_NAME
    return root if root.is_dir() else None


def seed_bundled_skills(profile_skills: Path,
                        bundled_root: Path | None = None) -> list[str]:
    """把内置池里 profile 还没有的技能整目录复制进去，返回新播种的技能名。

    以 SKILL.md 所在目录为一个技能；目标已存在的一律跳过。任何复制失败
    （只读盘、权限等）都静默略过——播种只是增强，不允许影响启动。
    """
    root = bundled_root if bundled_root is not None else bundled_skills_root()
    if root is None or not root.is_dir():
        return []
    seeded: list[str] = []
    for skill_md in sorted(root.rglob(SKILL_FILE)):
        src = skill_md.parent
        dest = profile_skills / src.relative_to(root)
        if dest.exists():
            continue
        try:
            shutil.copytree(src, dest)
            seeded.append(src.name)
        except OSError:
            continue
    return seeded


def register(ctx) -> None:
    host = ctx.host
    profile_skills = Path(host.profile.root) / "skills"
    # 先播种再声明：同一次启动内新技能即可被 agent 注册
    seed_bundled_skills(profile_skills)
    # 目录尚不存在时也声明——用户放入 SKILL.md 后重启即生效
    ctx.skills.add_dir(str(profile_skills))
