from __future__ import annotations

import mimetypes
from pathlib import Path


def guess_mime(path: Path | str) -> str:
    path = Path(path)
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return "image/jpeg"
    if suffix in {".mp4", ".mov", ".avi"}:
        return "video/mp4"
    return "application/octet-stream"


def split_text(text: str, limit: int = 2000) -> list[str]:
    """将长文本切分为渠道可发送的片段。"""
    content = text.strip()
    if not content:
        return [""]
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = min(start + limit, len(content))
        if end < len(content):
            split_at = content.rfind("\n", start, end)
            if split_at <= start:
                split_at = end
            end = split_at
        chunk = content[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks or [content[:limit]]
