#!/usr/bin/env python3
"""通过 xdg-desktop-portal 截图（GNOME Wayland 正确方式，勿用 grim / Shell D-Bus）。"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse


def portal_screenshot(dest: Path, *, interactive: bool = False) -> None:
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    log_path = dest.parent / f".portal-monitor-{dest.stem}.log"

    monitor = subprocess.Popen(
        [
            "gdbus",
            "monitor",
            "--session",
            "--dest",
            "org.freedesktop.portal.Desktop",
        ],
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.3)
        flag = "true" if interactive else "false"
        subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.portal.Desktop",
                "--object-path",
                "/org/freedesktop/portal/desktop",
                "--method",
                "org.freedesktop.portal.Screenshot.Screenshot",
                "",
                f"{{'interactive': <{flag}>}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        deadline = time.time() + 20
        while time.time() < deadline:
            if not log_path.exists():
                time.sleep(0.1)
                continue
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"Response\s*\([^)]*uint32\s+1\b", text):
                raise RuntimeError("截图已取消")
            if re.search(r"Response\s*\([^)]*uint32\s+2\b", text):
                raise RuntimeError("Portal 截图失败（权限或会话限制）")
            match = re.search(r"'uri':\s*<'(file://[^']+)'>", text)
            if not match:
                match = re.search(r"file://[^\s'\"\\)]+", text)
            if match:
                uri = match.group(1) if match.lastindex else match.group(0)
                src = Path(unquote(urlparse(uri).path))
                if src.is_file() and src.stat().st_size > 0:
                    dest.write_bytes(src.read_bytes())
                    return
            time.sleep(0.1)
        raise RuntimeError(
            "Portal 截图超时。若首次使用，请在本机图形终端运行: "
            f"python3 {Path(__file__).name} {dest} --interactive"
        )
    finally:
        monitor.terminate()
        monitor.wait(timeout=2)
        log_path.unlink(missing_ok=True)


def gnome_screenshot(dest: Path) -> None:
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["gnome-screenshot", "-f", str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "gnome-screenshot 失败")
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError(
            "gnome-screenshot 未生成文件（可能在无图形权限的终端中运行）"
        )


def is_gnome() -> bool:
    desktop = __import__("os").environ.get("XDG_CURRENT_DESKTOP", "")
    session = __import__("os").environ.get("DESKTOP_SESSION", "")
    return "GNOME" in desktop.upper() or "gnome" in session.lower()


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: portal-screenshot.py <输出.png> [--interactive]", file=sys.stderr)
        return 2

    dest = Path(sys.argv[1])
    interactive = "--interactive" in sys.argv[2:]

    try:
        if is_gnome():
            try:
                portal_screenshot(dest, interactive=interactive)
            except RuntimeError:
                if interactive:
                    raise
                portal_screenshot(dest, interactive=True)
        else:
            portal_screenshot(dest, interactive=interactive)
        print(dest.resolve())
        return 0
    except Exception as exc:
        # 最后尝试 gnome-screenshot（部分环境 portal 日志解析不到 uri）
        if is_gnome():
            try:
                gnome_screenshot(dest)
                print(dest.resolve())
                return 0
            except Exception as exc2:
                print(f"截图失败: {exc}; gnome-screenshot: {exc2}", file=sys.stderr)
                return 1
        print(f"截图失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
