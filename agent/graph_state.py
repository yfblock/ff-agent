"""Graph state for the LangGraph main-chat loop.

Dict-native: `messages` stays the exact list[dict] shape the rest of the app
uses (system/user/assistant/tool, multimodal content lists, reasoning_content),
so persistence (ChatHistoryStore), sanitize_messages_for_api, and multimodal
WeChat input all keep working untouched. The graph mutates this state in place
across the agent<->tools loop.

The two side-channels (saved_memories, role_changes) live here so they survive
the agent_node<->tools_node hops and can be read when building the final reply's
notes block — mirroring the list args threaded through the old _handle_tool_call.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, TypedDict


class ChatState(TypedDict, total=False):
    # Canonical conversation, same dict shape as Agent.messages.
    messages: list[dict[str, Any]]
    # Original user text for this turn, used to rebuild the system prompt
    # (memory search is query-relevant) every iteration.
    user_query: str
    # Side-channels appended to by tool handlers, read at turn end for notes.
    saved_memories: list[str]
    role_changes: list[str]
    # How many tool batches have run this turn (max-step cap guard).
    step_count: int
    # The streaming event callback. Carried in state (not config) because the
    # main-chat graph runs in-memory with no checkpointer, and LangGraph's
    # config-injection is unreliable for nested closure nodes. Not serialized.
    on_event: Optional[Callable[[dict[str, Any]], None]]
    # Internal: tool calls pending execution between agent and tools nodes.
    _pending_tool_calls: list[Any]
