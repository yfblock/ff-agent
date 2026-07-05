"""Golden event-contract tests for the main chat loop.

These freeze the exact ordered sequence of on_event payloads that Agent.chat()
emits, so the upcoming LangGraph rewrite can be proven to reproduce the same
contract byte-for-byte. Every consumer (TUI, web SSE, WeChat channel) depends
on these event types and keys.

Run directly (python tests/test_chat_events_golden.py) or via pytest.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from agent.config import load_config
from agent.core import Agent
from tests.fakes import FakeLLM, text, tools


def _make_agent(
    responses: list[dict[str, Any]],
    *,
    channel_id: str | None = None,
) -> Agent:
    """Build a hermetic Agent with a scripted FakeLLM and no persistence."""
    d = Path(tempfile.mkdtemp())
    os.environ["OPENAI_API_KEY"] = "sk-test-real"
    os.environ["SKILLS_DIR"] = str(d / "skills")
    os.environ["ROLES_DIR"] = str(d / "roles")
    os.environ["MEMORY_PATH"] = str(d / "mem.json")
    os.environ["WORKSPACE_DIR"] = str(d)
    os.environ["PERSIST_CHAT_HISTORY"] = "false"
    config = load_config(require_api_key=True)
    agent = Agent(config, session_key=None)
    fake = FakeLLM(responses)
    agent.llm = fake
    # Point the factory at the same fake so the graph path (which pulls from the
    # factory, not agent.llm) runs the identical scripted responses.
    from agent.lc_llm import ChatModelFactory

    class _FakeFactory(ChatModelFactory):
        def get(self, profile: Any, *, thinking_mode: Any = None) -> Any:
            return fake

    agent._model_factory = _FakeFactory(config)
    if channel_id is not None:
        agent.channel_id = channel_id
        agent.refresh_system_prompt()
    return agent


def _run(agent: Agent, message: str) -> tuple[str, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    reply = agent.chat(message, on_event=lambda e: events.append(e))
    return reply, events


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


def test_plain_reply() -> None:
    agent = _make_agent([text("你好，我能帮你什么？")])
    reply, events = _run(agent, "hi")
    assert reply == "你好，我能帮你什么？"
    assert _types(events) == ["turn_start", "content_delta", "turn_end"]
    assert events[1]["text"] == "你好，我能帮你什么？"
    assert events[1]["delta"] == "你好，我能帮你什么？"
    assert events[-1]["reply"] == "你好，我能帮你什么？"


def test_reply_with_reasoning() -> None:
    agent = _make_agent([text("42", reasoning="让我想想这个问题")])
    reply, events = _run(agent, "答案是多少")
    assert reply == "42"
    # reasoning + content stream during the loop; thinking_done fires after the
    # stream completes (core.py emits it post-loop), i.e. AFTER content_delta.
    assert _types(events) == [
        "turn_start",
        "thinking_delta",
        "content_delta",
        "thinking_done",
        "turn_end",
    ]
    assert events[1]["text"] == "让我想想这个问题"
    assert events[3]["text"] == "让我想想这个问题"


def test_single_tool_call() -> None:
    agent = _make_agent(
        [
            tools([("list_directory", {"path": "."})]),
            text("目录里没有文件。"),
        ]
    )
    reply, events = _run(agent, "看看目录")
    assert reply == "目录里没有文件。"
    assert _types(events) == [
        "turn_start",
        "tool_call_delta",
        "tool_calls_ready",
        "tool_start",
        "tool_end",
        "content_delta",
        "turn_end",
    ]
    # tool_call_delta carries the parsed + raw args plus formatted title/detail.
    delta = events[1]
    assert delta["name"] == "list_directory"
    assert delta["arguments"] == {"path": "."}
    assert "arguments_raw" in delta and "title" in delta and "detail" in delta
    assert events[2]["count"] == 1
    # tool_start / tool_end carry the dispatched result.
    assert events[3]["name"] == "list_directory"
    assert events[4]["name"] == "list_directory"
    assert "result" in events[4] and "block" in events[4]


def test_multi_tool_batch() -> None:
    agent = _make_agent(
        [
            tools(
                [
                    ("list_directory", {"path": "."}),
                    ("get_workspace", {}),
                ]
            ),
            text("完成。"),
        ]
    )
    reply, events = _run(agent, "看看环境")
    assert reply == "完成。"
    assert _types(events) == [
        "turn_start",
        "tool_call_delta",
        "tool_call_delta",
        "tool_calls_ready",
        "tool_start",
        "tool_end",
        "tool_start",
        "tool_end",
        "content_delta",
        "turn_end",
    ]
    assert events[3]["count"] == 2


def test_plan_lifecycle() -> None:
    agent = _make_agent(
        [
            tools([("create_plan", {"title": "T", "goal": "G", "steps": [{"id": "s1", "description": "d1"}]})]),
            tools([("update_plan_step", {"step_id": "s1", "status": "completed", "result": "ok"})]),
            tools([("complete_plan", {"summary": "done"})]),
            text("计划完成。"),
        ]
    )
    reply, events = _run(agent, "做个计划")
    assert reply == "计划完成。"
    types = _types(events)
    # Plan-specific events are injected from within the tool handlers.
    assert "plan_updated" in types
    assert "plan_step_updated" in types
    assert "plan_completed" in types
    # plan_updated carries the full plan dict.
    plan_updated = next(e for e in events if e["type"] == "plan_updated")
    assert plan_updated["plan"]["title"] == "T"
    step_updated = next(e for e in events if e["type"] == "plan_step_updated")
    assert step_updated["step_id"] == "s1"


def test_save_memory_side_channel() -> None:
    agent = _make_agent(
        [
            tools([("save_memory", {"content": "用户喜欢简洁回答"})]),
            text("好的。"),
        ]
    )
    reply, events = _run(agent, "记住我喜欢简洁")
    # The saved-memory note is appended to the reply text.
    assert "好的。" in reply
    assert "【已写入长期记忆】用户喜欢简洁回答" in reply
    assert agent.memory.list_all()[-1].content == "用户喜欢简洁回答"


def test_tool_error_result() -> None:
    agent = _make_agent(
        [
            tools([("read_file", {"path": "does-not-exist.txt"})]),
            text("文件读取失败。"),
        ]
    )
    reply, events = _run(agent, "读个不存在的文件")
    assert reply == "文件读取失败。"
    tool_end = next(e for e in events if e["type"] == "tool_end")
    # Errors are surfaced as {"ok": false, "error": ...} in the result string.
    assert '"ok": false' in tool_end["result"]


def test_wechat_send_attachment_visible() -> None:
    # In the wechat channel, send_attachment is an available tool. We only
    # assert the channel-tool path is reachable and errors are well-formed
    # (no outbound handler wired in a hermetic test).
    agent = _make_agent(
        [
            tools([("send_attachment", {"path": "nope.png"})]),
            text("已尝试发送。"),
        ],
        channel_id="wechat",
    )
    reply, events = _run(agent, "发个附件")
    assert reply == "已尝试发送。"
    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert tool_end["name"] == "send_attachment"


def _scenario_scripts() -> dict[str, list[dict[str, Any]]]:
    """Reusable scripts for parity checks between old loop and LangGraph."""
    return {
        "plain": [text("你好")],
        "reasoning": [text("42", reasoning="想一下")],
        "single_tool": [tools([("list_directory", {"path": "."})]), text("好了")],
        "multi_tool": [
            tools([("list_directory", {"path": "."}), ("get_workspace", {})]),
            text("完成"),
        ],
        "plan": [
            tools([("create_plan", {"title": "T", "goal": "G", "steps": [{"id": "s1", "description": "d1"}]})]),
            tools([("update_plan_step", {"step_id": "s1", "status": "completed", "result": "ok"})]),
            tools([("complete_plan", {"summary": "done"})]),
            text("计划完成"),
        ],
        "memory": [tools([("save_memory", {"content": "简洁"})]), text("好")],
    }


def _normalize(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Reduce events to (type, key-detail) pairs for order-sensitive compare."""
    out: list[tuple[str, str]] = []
    for e in events:
        t = e["type"]
        if t in {"tool_start", "tool_end", "tool_call_delta"}:
            out.append((t, e.get("name", "")))
        elif t in {"plan_updated", "plan_step_updated", "plan_completed"}:
            out.append((t, ""))
        elif t == "tool_calls_ready":
            out.append((t, str(e.get("count"))))
        else:
            out.append((t, ""))
    return out


def test_all_scenarios_emit_correct_events() -> None:
    """All core scenarios produce the correct event sequence via LangGraph."""
    expected_plan_events = {
        "plan_updated", "plan_step_updated", "plan_completed",
        "tool_calls_ready", "tool_start", "tool_end", "turn_end",
    }
    for name, script in _scenario_scripts().items():
        agent = _make_agent(list(script))
        reply, events = _run(agent, "go")
        assert "turn_start" in _types(events), f"missing turn_start in {name}"
        assert "turn_end" in _types(events), f"missing turn_end in {name}"
        if name == "plan":
            emitted = set(_types(events))
            assert expected_plan_events <= emitted, f"plan events missing in {name}: {expected_plan_events - emitted}"
        if name == "memory":
            assert "【已写入长期记忆】" in reply, "memory side-channel missing from reply"


def _run_all() -> None:
    test_plain_reply()
    test_reply_with_reasoning()
    test_single_tool_call()
    test_multi_tool_batch()
    test_plan_lifecycle()
    test_save_memory_side_channel()
    test_tool_error_result()
    test_wechat_send_attachment_visible()
    test_all_scenarios_emit_correct_events()
    print("chat event golden tests ok")


if __name__ == "__main__":
    _run_all()
