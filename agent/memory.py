from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class MemoryItem:
    content: str
    created_at: str = field(default_factory=lambda: _now())
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> MemoryItem:
        return cls(
            content=str(data.get("content", "")),
            created_at=str(data.get("created_at") or _now()),
            tags=list(data.get("tags") or []),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self, path: Path, max_items: int = 50):
        self.path = path
        self.max_items = max_items
        self.items: list[MemoryItem] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.items = []
            return

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.items = [MemoryItem.from_dict(item) for item in data.get("items", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"items": [asdict(item) for item in self.items[-self.max_items :]]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, content: str, tags: list[str] | None = None) -> MemoryItem:
        item = MemoryItem(content=content.strip(), tags=tags or [])
        if not item.content:
            raise ValueError("记忆内容不能为空")
        self.items.append(item)
        self.items = self.items[-self.max_items :]
        self.save()
        return item

    def remove(self, index: int) -> MemoryItem:
        if index < 0 or index >= len(self.items):
            raise IndexError("记忆索引不存在")
        item = self.items.pop(index)
        self.save()
        return item

    def list_all(self) -> list[MemoryItem]:
        return list(self.items)

    def search(self, query: str, limit: int = 8) -> list[MemoryItem]:
        if not query.strip():
            return self.items[-limit:]

        tokens = _tokenize(query)
        scored: list[tuple[int, MemoryItem]] = []
        for item in self.items:
            text = f"{item.content} {' '.join(item.tags)}".lower()
            score = sum(1 for token in tokens if token in text)
            if score:
                scored.append((score, item))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[\W_]+", text.lower()) if len(t) >= 2]


def build_memory_prompt(items: list[MemoryItem]) -> str:
    if not items:
        return "暂无相关长期记忆。"

    lines = []
    for idx, item in enumerate(items, start=1):
        tag_text = f" [{', '.join(item.tags)}]" if item.tags else ""
        lines.append(f"{idx}. {item.content}{tag_text}")
    return "\n".join(lines)


def format_memory_list(items: list[MemoryItem]) -> str:
    if not items:
        return "暂无长期记忆。"

    lines = []
    for idx, item in enumerate(items):
        tags = f" [{', '.join(item.tags)}]" if item.tags else ""
        lines.append(f"{idx}. {item.content}{tags}")
    return "\n".join(lines)


MEMORY_COMMAND_HELP = """长期记忆命令:
  /memory              列出全部
  /memory add 内容     手动添加
  /memory delete 序号  删除指定条（序号见列表左侧）"""
