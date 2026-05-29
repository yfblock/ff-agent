from __future__ import annotations

from copy import deepcopy
from typing import Any


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if call_id:
            ids.append(str(call_id))
    return ids


def _strip_tool_calls(message: dict[str, Any]) -> dict[str, Any] | None:
    cleaned = {key: value for key, value in message.items() if key != "tool_calls"}
    content = cleaned.get("content")
    reasoning = cleaned.get("reasoning_content")
    if (isinstance(content, str) and content.strip()) or (
        isinstance(reasoning, str) and reasoning.strip()
    ):
        return cleaned
    return None


def sanitize_messages_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """修复截断或并发同步导致的无效 tool 消息序列。"""
    sanitized: list[dict[str, Any]] = []
    index = 0

    while index < len(messages):
        message = messages[index]
        role = message.get("role")

        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            if (
                sanitized
                and sanitized[-1].get("role") == "assistant"
                and tool_call_id in _tool_call_ids(sanitized[-1])
            ):
                sanitized.append(message)
            index += 1
            continue

        if role == "assistant" and message.get("tool_calls"):
            expected_ids = _tool_call_ids(message)
            following: list[dict[str, Any]] = []
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].get("role") == "tool":
                following.append(messages[cursor])
                cursor += 1

            if expected_ids:
                responded = {
                    str(item.get("tool_call_id"))
                    for item in following
                    if item.get("tool_call_id")
                }
                if all(call_id in responded for call_id in expected_ids):
                    sanitized.append(message)
                    sanitized.extend(following)
                    index = cursor
                    continue

            stripped = _strip_tool_calls(message)
            if stripped is not None:
                sanitized.append(stripped)
            index += 1
            continue

        sanitized.append(message)
        index += 1

    return sanitized


def truncate_messages(
    messages: list[dict[str, Any]],
    max_messages: int,
) -> list[dict[str, Any]]:
    if max_messages <= 0:
        return []
    if len(messages) <= max_messages:
        return sanitize_messages_for_api(messages)

    trimmed = deepcopy(messages[-max_messages:])
    while trimmed and trimmed[0].get("role") == "tool":
        trimmed.pop(0)

    if trimmed and trimmed[0].get("role") == "assistant" and trimmed[0].get("tool_calls"):
        expected_ids = _tool_call_ids(trimmed[0])
        if expected_ids:
            responded = {
                str(item.get("tool_call_id"))
                for item in trimmed[1:]
                if item.get("role") == "tool" and item.get("tool_call_id")
            }
            if not all(call_id in responded for call_id in expected_ids):
                stripped = _strip_tool_calls(trimmed[0])
                trimmed = ([stripped] if stripped else []) + trimmed[1:]

    return sanitize_messages_for_api(trimmed)
