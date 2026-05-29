from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class DisplayEntry:
    type: str
    text: str
    created_at: str = field(default_factory=lambda: _now())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DisplayEntry:
        return cls(
            type=str(data.get("type") or "system"),
            text=str(data.get("text") or ""),
            created_at=str(data.get("created_at") or _now()),
        )


from agent.messages_util import sanitize_messages_for_api, truncate_messages


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_history_path(base_path: Path, sessions_dir: Path, session_key: str) -> Path:
    if session_key == "local":
        return base_path
    safe = re.sub(r"[^\w\-.:@]+", "_", session_key)
    return sessions_dir / f"{safe}.json"


class ChatHistoryStore:
    """持久化 LLM 消息与 TUI 展示记录。"""

    def __init__(self, path: Path, *, max_messages: int = 200, max_display: int = 500):
        self.path = path
        self.max_messages = max_messages
        self.max_display = max_display
        self.messages: list[dict[str, Any]] = []
        self.display: list[DisplayEntry] = []
        self.current_role_name: str | None = None
        self.workspace_dir: str | None = None
        self.active_plan: dict[str, Any] | None = None
        self.current_model: str | None = None
        self.load()

    def _reset_loaded_state(self) -> None:
        self.messages = []
        self.display = []
        self.current_role_name = None
        self.workspace_dir = None
        self.active_plan = None
        self.current_model = None

    def _apply_loaded_data(self, data: dict[str, Any]) -> None:
        self.messages = sanitize_messages_for_api(list(data.get("messages") or []))
        self.display = [
            DisplayEntry.from_dict(item) for item in (data.get("display") or [])
        ]
        role = data.get("current_role_name")
        self.current_role_name = str(role) if role else None
        workspace = data.get("workspace_dir")
        self.workspace_dir = str(workspace) if workspace else None
        plan = data.get("active_plan")
        self.active_plan = plan if isinstance(plan, dict) else None
        model = data.get("current_model")
        self.current_model = str(model) if model else None

    def load(self) -> None:
        if not self.path.exists():
            self._reset_loaded_state()
            return

        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return

        raw = raw.strip()
        if not raw:
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if not isinstance(data, dict):
            return

        self._apply_loaded_data(data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _now(),
            "current_role_name": self.current_role_name,
            "workspace_dir": self.workspace_dir,
            "active_plan": self.active_plan,
            "current_model": self.current_model,
            "messages": truncate_messages(self.messages, self.max_messages),
            "display": [asdict(item) for item in self.display[-self.max_display :]],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, self.path)

    def clear(self) -> None:
        self.messages = []
        self.display = []
        self.current_role_name = None
        self.save()

    def set_workspace_dir(self, path: Path | str | None) -> None:
        self.workspace_dir = str(path) if path else None
        self.save()

    def set_active_plan(self, plan: dict[str, Any] | None) -> None:
        self.active_plan = plan
        self.save()

    def set_current_model(self, model: str | None) -> None:
        self.current_model = model
        self.save()

    def touch_metadata(self) -> None:
        self.save()

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = truncate_messages(messages, self.max_messages)
        self.save()

    def record_display(self, entry_type: str, text: str) -> None:
        if not text.strip():
            return
        self.display.append(DisplayEntry(type=entry_type, text=text))
        self.display = self.display[-self.max_display :]
        self.save()

    def display_items(self) -> list[DisplayEntry]:
        return list(self.display)
