from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    disable_model_invocation: bool = False

    def to_prompt_block(self) -> str:
        return (
            f"### Skill: {self.name}\n"
            f"**Description**: {self.description}\n\n"
            f"{self.body.strip()}\n"
        )


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_skill_file(path: Path) -> Skill | None:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None

    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()
    name = str(meta.get("name") or path.parent.name)
    description = str(meta.get("description") or "").strip()
    disable = bool(meta.get("disable-model-invocation", False))

    return Skill(
        name=name,
        description=description,
        path=path,
        body=body,
        disable_model_invocation=disable,
    )


def load_skills(skills_dirs: Path | Sequence[Path]) -> list[Skill]:
    if isinstance(skills_dirs, Path):
        dirs = [skills_dirs]
    else:
        dirs = list(skills_dirs)

    skills: list[Skill] = []
    index_by_name: dict[str, int] = {}

    for skills_dir in dirs:
        if not skills_dir.exists():
            continue
        for skill_file in sorted(skills_dir.rglob("SKILL.md")):
            skill = _parse_skill_file(skill_file)
            if not skill or skill.disable_model_invocation:
                continue
            if skill.name in index_by_name:
                skills[index_by_name[skill.name]] = skill
            else:
                index_by_name[skill.name] = len(skills)
                skills.append(skill)
    return skills


def build_skills_prompt(skills: list[Skill]) -> str:
    if not skills:
        return "当前没有可用的 skills。"

    blocks = [skill.to_prompt_block() for skill in skills]
    return "\n".join(blocks)


def format_skills_list(
    skills: list[Skill],
    skills_dirs: Path | Sequence[Path],
) -> str:
    if isinstance(skills_dirs, Path):
        dirs = [skills_dirs]
    else:
        dirs = list(skills_dirs)

    dirs_text = ", ".join(str(path) for path in dirs)
    if not skills:
        return f"以下目录中没有可用的 skills: {dirs_text}"

    lines = [f"共 {len(skills)} 个 skill（目录: {dirs_text}）:", ""]
    for idx, skill in enumerate(skills, start=1):
        desc = skill.description or "(无描述)"
        lines.append(f"{idx}. {skill.name}")
        lines.append(f"   描述: {desc}")
        lines.append(f"   路径: {skill.path}")
        lines.append("")
    return "\n".join(lines).rstrip()
