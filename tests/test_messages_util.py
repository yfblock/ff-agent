from __future__ import annotations

from agent.messages_util import sanitize_messages_for_api, truncate_messages


def test_orphan_tool_is_removed() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]
    cleaned = sanitize_messages_for_api(messages)
    assert cleaned == [{"role": "user", "content": "hi"}]


def test_complete_tool_sequence_is_kept() -> None:
    messages = [
        {"role": "user", "content": "run"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        {"role": "assistant", "content": "done"},
    ]
    cleaned = sanitize_messages_for_api(messages)
    assert len(cleaned) == 4


def test_incomplete_tool_turn_is_stripped() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }
            ],
        }
    ]
    cleaned = sanitize_messages_for_api(messages)
    assert cleaned == [{"role": "assistant", "content": "thinking"}]


def test_truncate_drops_leading_orphan_tools() -> None:
    messages = [
        {"role": "user", "content": "a"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        {"role": "assistant", "content": "ok"},
    ]
    trimmed = truncate_messages(messages, 2)
    assert trimmed[0]["role"] != "tool"
