"""Golden parity tests for /discuss: old runner vs LangGraph graph.

Both paths must emit the same discuss_* event sequence for: consensus reached,
NEED_USER pause+resume, and user stop. Participants are driven by per-profile
FakeLLM clients (non-streaming chat()). The status line (CONSENSUS/NEED_USER/
CONTINUE) drives control flow and must be reproduced identically.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from agent.discuss import DiscussRunner, DiscussState
from agent.tools import DISCUSS_READ_TOOLS
from agent.model_settings import ModelProfile
from tests.fakes import FakeLLM, text


def _config():
    d = Path(tempfile.mkdtemp())
    os.environ["OPENAI_API_KEY"] = "sk-test"
    os.environ["WORKSPACE_DIR"] = str(d)
    from agent.config import load_config

    return load_config(require_api_key=True)


def _profile(name: str) -> ModelProfile:
    return ModelProfile(name=name, model=name, base_url="https://api.example.com", api_key="sk")


def _make_runner(
    scripts: dict[str, list[dict[str, Any]]],
    *,
    profiles: list[str],
    answers: list[str] | None = None,
):
    config = _config()
    state = DiscussState(
        topic="设计缓存",
        profiles=tuple(_profile(p) for p in profiles),
        workspace_context="workspace: /tmp/x",
    )
    events: list[dict[str, Any]] = []
    fakes = {name: FakeLLM(list(script)) for name, script in scripts.items()}
    answer_iter = iter(answers or [])

    def wait_for_user(question: str) -> str:
        return next(answer_iter, "补充信息")

    runner = DiscussRunner(
        config=config,
        state=state,
        stop_event=threading.Event(),
        wait_for_user=wait_for_user,
        handle_tool=lambda name, args: '{"ok": true}',
        tools=DISCUSS_READ_TOOLS,
        on_event=lambda e: events.append(e),
        max_rounds=5,
    )
    runner._client_for = lambda profile: fakes[profile.name]  # type: ignore[method-assign]
    return runner, events


def _norm(events: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for e in events:
        t = e["type"]
        if t in {"discuss_turn", "discuss_turn_start"}:
            out.append((t, e.get("profile")))
        elif t == "discuss_round":
            out.append((t, e.get("round")))
        else:
            out.append((t, None))
    return out


def test_discuss_parity_consensus() -> None:
    # Both participants reach consensus in round 1.
    scripts = {
        "a": [text("CONSENSUS: 用 Redis\n\n理由A")],
        "b": [text("CONSENSUS: 用 Redis\n\n理由B")],
    }
    old_runner, old_events = _make_runner(scripts, profiles=["a", "b"])
    old_result = old_runner.run()

    graph_runner, graph_events = _make_runner(scripts, profiles=["a", "b"])
    from agent.graph_discuss import run_discuss_graph

    graph_result = run_discuss_graph(graph_runner)
    assert _norm(old_events) == _norm(graph_events)
    assert old_result.kind == graph_result.kind == "consensus"


def test_discuss_parity_need_user_then_consensus() -> None:
    # Round 1: 'a' asks the user (NEED_USER) -> pause. Round 1 re-runs (like the
    # old `continue`): both reach consensus.
    scripts = {
        "a": [text("NEED_USER: 请提供 QPS 目标\n细节"), text("CONSENSUS: 方案\n\nA")],
        "b": [text("CONSENSUS: 方案\n\nB")],
    }
    old_runner, old_events = _make_runner(scripts, profiles=["a", "b"], answers=["QPS 1000"])
    old_result = old_runner.run()

    scripts2 = {
        "a": [text("NEED_USER: 请提供 QPS 目标\n细节"), text("CONSENSUS: 方案\n\nA")],
        "b": [text("CONSENSUS: 方案\n\nB")],
    }
    graph_runner, graph_events = _make_runner(scripts2, profiles=["a", "b"], answers=["QPS 1000"])
    from agent.graph_discuss import run_discuss_graph

    graph_result = run_discuss_graph(graph_runner)
    assert _norm(old_events) == _norm(graph_events)
    assert old_result.kind == graph_result.kind
    assert any(e["type"] == "discuss_need_user" for e in graph_events)
    assert any(e["type"] == "discuss_user_supplement" for e in graph_events)


def test_discuss_parity_stop() -> None:
    scripts = {"a": [text("CONTINUE: 再想想\nA")], "b": [text("CONTINUE: 再想想\nB")]}
    old_runner, old_events = _make_runner(scripts, profiles=["a", "b"])
    old_runner.stop_event.set()
    old_result = old_runner.run()

    scripts2 = {"a": [text("CONTINUE: 再想想\nA")], "b": [text("CONTINUE: 再想想\nB")]}
    graph_runner, graph_events = _make_runner(scripts2, profiles=["a", "b"])
    graph_runner.stop_event.set()
    from agent.graph_discuss import run_discuss_graph

    graph_result = run_discuss_graph(graph_runner)
    assert _norm(old_events) == _norm(graph_events)
    assert old_result.kind == graph_result.kind == "stopped"


def _run_all() -> None:
    test_discuss_parity_consensus()
    test_discuss_parity_need_user_then_consensus()
    test_discuss_parity_stop()
    print("discuss graph parity tests ok")


if __name__ == "__main__":
    _run_all()
