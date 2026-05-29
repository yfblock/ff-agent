from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_ROLE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


@dataclass
class Role:
    name: str
    title: str
    description: str
    path: Path
    body: str

    def to_prompt_block(self) -> str:
        heading = self.title or self.name
        desc = f"\n**简介**: {self.description}" if self.description else ""
        return f"## 当前身份: {heading}{desc}\n\n{self.body.strip()}\n"


def validate_role_name(name: str) -> str:
    name = name.strip().lower()
    if not name or not _ROLE_NAME_RE.match(name):
        raise ValueError(
            "role name 只能使用小写字母、数字和连字符，且不能以连字符开头或结尾"
        )
    return name


def render_role_markdown(
    name: str,
    title: str,
    description: str,
    body: str,
) -> str:
    frontmatter = {
        "name": name,
        "title": title.strip() or name,
        "description": description.strip(),
    }
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    return f"---\n{yaml_text}\n---\n\n{body.strip()}\n"


def _parse_role_file(path: Path) -> Role | None:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None

    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()
    name = str(meta.get("name") or path.parent.name)
    title = str(meta.get("title") or name).strip()
    description = str(meta.get("description") or "").strip()

    return Role(
        name=name,
        title=title,
        description=description,
        path=path,
        body=body,
    )


def load_roles(roles_dir: Path) -> list[Role]:
    if not roles_dir.exists():
        return []

    roles: list[Role] = []
    index_by_name: dict[str, int] = {}
    for role_file in sorted(roles_dir.rglob("ROLE.md")):
        role = _parse_role_file(role_file)
        if not role:
            continue
        if role.name in index_by_name:
            roles[index_by_name[role.name]] = role
        else:
            index_by_name[role.name] = len(roles)
            roles.append(role)
    return roles


def get_role(roles: list[Role], name: str) -> Role | None:
    for role in roles:
        if role.name == name:
            return role
    return None


def save_role_file(
    roles_dir: Path,
    name: str,
    title: str,
    description: str,
    body: str,
) -> Role:
    name = validate_role_name(name)
    if not body.strip():
        raise ValueError("role 正文 body 不能为空")

    roles_dir.mkdir(parents=True, exist_ok=True)
    role_dir = roles_dir / name
    role_dir.mkdir(parents=True, exist_ok=True)
    path = role_dir / "ROLE.md"
    path.write_text(
        render_role_markdown(name, title, description, body),
        encoding="utf-8",
    )

    role = _parse_role_file(path)
    if role is None:
        raise ValueError(f"写入 role 失败: {path}")
    return role


def delete_role_file(roles_dir: Path, name: str) -> Role:
    name = validate_role_name(name)
    role = get_role(load_roles(roles_dir), name)
    if role is None:
        raise ValueError(f"role 不存在: {name}")

    shutil.rmtree(role.path.parent)
    return role


def build_role_prompt(role: Role | None) -> str:
    if role is None:
        return "## 当前身份: 通用助手\n\n以清晰、准确、友好的方式回答用户问题。"
    return role.to_prompt_block()


ROLE_COMMAND_HELP = """Role 命令（切换身份，保留聊天历史）:
  /role              列出全部 role，标记当前身份
  /role <name>       切换到指定 role
  /role reload       重新加载 roles 目录
  /role help         显示此帮助"""


def format_roles_list(roles: list[Role], current: str | None, roles_dir: Path) -> str:
    if not roles:
        return f"目录 {roles_dir} 下没有可用的 roles。"

    lines = [f"共 {len(roles)} 个 role（目录: {roles_dir}）:", ""]
    for role in roles:
        marker = " (当前)" if role.name == current else ""
        title = role.title or role.name
        desc = role.description or "(无描述)"
        lines.append(f"- {role.name}{marker}")
        lines.append(f"  名称: {title}")
        lines.append(f"  描述: {desc}")
        lines.append(f"  路径: {role.path}")
        lines.append("")
    lines.append("切换示例: /role coder")
    return "\n".join(lines).rstrip()
