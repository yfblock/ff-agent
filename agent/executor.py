from __future__ import annotations

import subprocess
from pathlib import Path


class WorkspaceExecutor:
    """在工作区内安全执行文件读写与 shell 命令。"""

    def __init__(
        self,
        workspace: Path,
        *,
        allow_shell: bool = True,
        command_timeout: int = 60,
    ) -> None:
        self.workspace = workspace.resolve()
        self.allow_shell = allow_shell
        self.command_timeout = command_timeout

    def set_workspace(self, workspace: Path) -> None:
        resolved = workspace.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"不是目录: {resolved}")
        self.workspace = resolved

    def _resolve_path(self, path: str) -> Path:
        if not path or not str(path).strip():
            raise ValueError("路径不能为空")

        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()

        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError(f"路径超出工作区范围: {path}") from exc

        return resolved

    def read_file(self, path: str, *, max_chars: int = 120_000) -> str:
        target = self._resolve_path(path)
        if not target.is_file():
            raise ValueError(f"文件不存在: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n…(已截断，共 {len(content)} 字符)"
        return content

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target.relative_to(self.workspace))

    def resolve_file(self, path: str) -> Path:
        target = self._resolve_path(path)
        if not target.is_file():
            raise ValueError(f"文件不存在: {path}")
        return target

    def list_directory(self, path: str = ".") -> list[str]:
        target = self._resolve_path(path)
        if not target.is_dir():
            raise ValueError(f"目录不存在: {path}")

        entries: list[str] = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            rel = item.relative_to(self.workspace)
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{rel}{suffix}")
        return entries

    def run_command(self, command: str, *, cwd: str | None = None) -> dict[str, str | int]:
        if not self.allow_shell:
            raise ValueError("当前配置禁用了 shell 执行（ALLOW_SHELL=false）")
        if not command.strip():
            raise ValueError("命令不能为空")

        workdir = self.workspace if cwd in (None, "", ".") else self._resolve_path(cwd)
        if not workdir.is_dir():
            raise ValueError(f"工作目录不存在: {cwd}")

        completed = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=self.command_timeout,
            check=False,
        )

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if len(stdout) > 40_000:
            stdout = stdout[:40_000] + "\n…(stdout 已截断)"
        if len(stderr) > 20_000:
            stderr = stderr[:20_000] + "\n…(stderr 已截断)"

        return {
            "command": command,
            "cwd": str(workdir.relative_to(self.workspace)),
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
