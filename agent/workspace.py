from __future__ import annotations

from pathlib import Path


def resolve_workspace(path: str, *, default: Path | None = None) -> Path:
    text = (path or "").strip()
    if not text:
        raise ValueError("工作区路径不能为空")

    if text.lower() in {"default", ".env", "reset"}:
        if default is None:
            raise ValueError("未配置默认工作区")
        resolved = default.expanduser().resolve()
    else:
        resolved = Path(text).expanduser().resolve()

    if not resolved.exists():
        raise ValueError(f"路径不存在: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"不是目录: {resolved}")
    return resolved


def format_workspace_path(path: Path) -> str:
    try:
        home = Path.home()
        if path == home:
            return "~"
        if home in path.parents or path.is_relative_to(home):
            return f"~/{path.relative_to(home)}"
    except (ValueError, RuntimeError):
        pass
    return str(path)


WORKSPACE_COMMAND_HELP = """工作区命令:
  /workspace              查看当前工作区
  /workspace <路径>       切换到指定目录
  /workspace default      恢复 .env 中的 WORKSPACE_DIR"""
