"""LangGraph main-chat loop: a hand-rolled 2-node StateGraph.

Reproduces Agent._chat_locked's behavior and its EXACT event contract, while
adding a max-step cap (the old while-True loop had none). Node layout:

    START -> agent_node -> (tool_calls & under cap) -> tools_node -> agent_node
                        -> (else) -> END

agent_node streams one model turn via ChatModelFactory (openai-SDK engine, so
DeepSeek reasoning survives), forwarding the same thinking_delta/content_delta/
tool_call_delta events the old loop emitted, then appends the serialized
assistant message. tools_node runs the tool batch via ToolExecutor (which emits
tool_start/tool_end/plan_* through the reused _handle_tool_call).

on_event is passed per-invocation via config["configurable"]["on_event"] so a
single compiled graph is thread-safe across concurrent turns.
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from agent.graph_state import ChatState
from agent.graph_tools import ToolExecutor
from agent.llm import serialize_assistant_message
from agent.model_settings import ModelProfile
from agent.tool_display import format_tool_detail, format_tool_title, try_parse_tool_args


def _emit(on_event: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if on_event:
        on_event(payload)


def _current_profile(agent: Any) -> ModelProfile:
    """The ModelProfile matching the agent's live model selection."""
    return agent.get_model_profile(agent.current_profile_name)


def build_chat_graph(agent: Any):
    """Compile the main-chat graph bound to a specific Agent instance."""
    tool_executor = ToolExecutor(agent)

    def agent_node(state: ChatState) -> ChatState:
        on_event = state.get("on_event")

        # Rebuild the system prompt from the live query every iteration (covers
        # both turn-start and post-tool cases, like core.py's two refresh points).
        agent.refresh_system_prompt(state.get("user_query", ""))

        messages = state["messages"]
        profile = _current_profile(agent)
        factory = agent.model_factory

        reasoning_text = ""
        content_text = ""
        saw_thinking_delta = False
        message = None

        for event in factory.stream_events(profile, messages, tools=agent._available_tools()):
            etype = event.get("type")
            if etype == "thinking_delta":
                saw_thinking_delta = True
                reasoning_text = str(event.get("text") or "")
                _emit(on_event, {
                    "type": "thinking_delta",
                    "text": reasoning_text,
                    "delta": event.get("delta", ""),
                })
            elif etype == "content_delta":
                content_text = str(event.get("text") or "")
                _emit(on_event, {
                    "type": "content_delta",
                    "text": content_text,
                    "delta": event.get("delta", ""),
                })
            elif etype == "tool_call_delta":
                name = str(event.get("name") or "")
                raw = str(event.get("arguments") or "")
                args = try_parse_tool_args(raw)
                _emit(on_event, {
                    "type": "tool_call_delta",
                    "name": name,
                    "arguments": args,
                    "arguments_raw": raw,
                    "title": format_tool_title(name, args),
                    "detail": format_tool_detail(name, args),
                })
            elif etype == "message_complete":
                message = event.get("message")
                reasoning_text = str(event.get("reasoning") or reasoning_text)
                content_text = str(event.get("content") or content_text)

        if message is None:
            raise RuntimeError("LLM 流式响应未返回完整消息")

        # thinking_done fires post-stream (matches core.py ordering: after content).
        if reasoning_text:
            if not saw_thinking_delta:
                _emit(on_event, {
                    "type": "thinking_delta",
                    "text": reasoning_text,
                    "delta": reasoning_text,
                })
            _emit(on_event, {"type": "thinking_done", "text": reasoning_text})

        assistant_msg = serialize_assistant_message(message)
        messages.append(assistant_msg)

        if message.tool_calls:
            _emit(on_event, {"type": "tool_calls_ready", "count": len(message.tool_calls)})
            state["_pending_tool_calls"] = list(message.tool_calls)
        else:
            # Explicit empty list, not pop(): LangGraph's LastValue channel keeps
            # the prior value if a key is merely removed from the returned dict.
            state["_pending_tool_calls"] = []
        return state

    def tools_node(state: ChatState) -> ChatState:
        on_event = state.get("on_event")
        tool_calls = state.get("_pending_tool_calls") or []
        tool_messages = tool_executor.run_batch(tool_calls, state, on_event)
        state["messages"].extend(tool_messages)
        state["step_count"] = state.get("step_count", 0) + 1
        state["_pending_tool_calls"] = []
        return state

    def should_continue(state: ChatState) -> str:
        if not state.get("_pending_tool_calls"):
            return END
        if state.get("step_count", 0) >= agent.config.max_chat_steps:
            # Cap hit: append a synthetic assistant note so the turn still ends
            # cleanly (and turn_end fires downstream). Behavior change vs the old
            # unbounded while-True loop — intentional safety fix.
            state["messages"].append({
                "role": "assistant",
                "content": f"（已达到最大工具调用步数 {agent.config.max_chat_steps}，停止执行。）",
            })
            state["_pending_tool_calls"] = []
            return END
        return "tools"

    graph = StateGraph(ChatState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()
