import json
from pathlib import Path

from agent.history import ChatHistoryStore


def test_load_tolerates_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "chat_history.json"
    path.write_text("", encoding="utf-8")

    store = ChatHistoryStore(path)
    assert store.messages == []
    assert store.display == []

    store.record_display("user", "hello")
    assert len(store.display) == 1


def test_load_keeps_memory_on_transient_empty_read(tmp_path: Path) -> None:
    path = tmp_path / "chat_history.json"
    store = ChatHistoryStore(path)
    store.record_display("user", "hello")
    assert len(store.display) == 1

    path.write_text("", encoding="utf-8")
    store.load()
    assert len(store.display) == 1


def test_save_is_readable_after_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "chat_history.json"
    store = ChatHistoryStore(path)
    store.record_display("assistant", "world")

    reloaded = ChatHistoryStore(path)
    assert len(reloaded.display) == 1
    assert reloaded.display[0].text == "world"

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["display"][0]["text"] == "world"
