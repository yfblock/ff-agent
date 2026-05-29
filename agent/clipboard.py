from __future__ import annotations

import shutil
import subprocess


def copy_to_system_clipboard(text: str) -> bool:
    """写入系统剪贴板；优先 wl-copy / xclip / xsel（GNOME Terminal 通常不支持 OSC 52）。"""
    if not text:
        return False

    commands: list[list[str]] = []
    if shutil.which("wl-copy"):
        commands.append(["wl-copy"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel"):
        commands.append(["xsel", "--clipboard", "--input"])

    data = text.encode("utf-8")
    for command in commands:
        try:
            subprocess.run(
                command,
                input=data,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (subprocess.CalledProcessError, OSError):
            continue
    return False
