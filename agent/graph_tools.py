"""Tool execution node for the LangGraph main-chat loop.

Deliberately does NOT split the 17 tools into separate StructuredTools. The
existing Agent._handle_tool_call is the single dispatch choke point: it closes
over memory/planner/executor/roles, mutates the saved_memories/role_changes
side-channels, emits tool_start/tool_end/plan_* events, and returns the JSON
string (including the {"ok": false, "error": ...} error shape) that becomes the
tool message content. We reuse it verbatim so the event/return contract is
identical to the old loop.
"""

from __future__ import annotations

from typing import Any, Callable

from agent.graph_state import ChatState


class ToolExecutor:
    """Runs a batch of tool calls by delegating to Agent._handle_tool_call."""

    def __init__(self, agent: Any):
        self._agent = agent

    def run_batch(
        self,
        tool_calls: list[Any],
        state: ChatState,
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> list[dict[str, Any]]:
        """Execute each tool call, returning the tool messages to append.

        `tool_calls` are the SimpleNamespace objects off the assistant message
        (call.id, call.function.name, call.function.arguments) — same shape the
        old loop iterated. Side-channels are read/written on `state` in place.
        """
        saved_memories = state.setdefault("saved_memories", [])
        role_changes = state.setdefault("role_changes", [])
        tool_messages: list[dict[str, Any]] = []
        for call in tool_calls:
            result = self._agent._handle_tool_call(
                call.function.name,
                call.function.arguments,
                saved_memories,
                role_changes,
                on_event,
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                }
            )
        return tool_messages
