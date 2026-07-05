"""Golden parity tests for the /assign pipeline: old runner vs LangGraph graph.

Both paths must emit the same sequence of assign_* events for the core
scenarios (clean done, reject->revise->done, stop). We drive AssignRunner with
per-profile FakeLLM clients (non-streaming chat()) and compare event streams.

Run directly or via pytest.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from agent.assign import AssignRunner, AssignJob, AssignState
from agent.model_settings import ModelProfile
from tests.fakes import FakeLLM, text, tools


def _config():
    d = Path(tempfile.mkdtemp())
    os.environ["OPENAI_API_KEY"] = "sk-test"
    os.environ["WORKSPACE_DIR"] = str(d)
    from agent.config import load_config

    return load_config(require_api_key=True)


def _profile(name: str) -> ModelProfile:
    return ModelProfile(name=name, model=name, base_url="https://api.example.com", api_key="sk")


def _make_runner(scripts: dict[str, list[dict[str, Any]]], *, jobs: list[tuple[str, str]]):
    """Build an AssignRunner whose per-profile client is a scripted FakeLLM."""
    config = _config()
    state = AssignState(
        jobs=tuple(AssignJob(profile_name=n, task=t, index=i) for i, (n, t) in enumerate(jobs)),
        workspace_context="workspace: /tmp/x",
    )
    events: list[dict[str, Any]] = []
    fakes = {name: FakeLLM(list(script)) for name, script in scripts.items()}
    runner = AssignRunner(
        config=config,
        state=state,
        stop_event=threading.Event(),
        handle_tool=lambda idx, name, args: '{"ok": true}',
        resolve_profile=_profile,
        on_event=lambda e: events.append(e),
        max_turns=6,
        max_tool_steps=4,
        max_rounds=3,
    )
    runner._client_for = lambda profile: fakes[profile.name]  # type: ignore[method-assign]
    return runner, events


def _norm(events: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for e in events:
        t = e["type"]
        if t == "assign_worker_done":
            out.append((t, e.get("outcome")))
        elif t == "assign_pipeline_step":
            out.append((t, e.get("step")))
        elif t == "assign_round_start":
            out.append((t, e.get("round")))
        else:
            out.append((t, None))
    return out


def _run_both(scripts, jobs):
    old_runner, old_events = _make_runner(scripts, jobs=jobs)
    old_result = old_runner.run()

    graph_runner, graph_events = _make_runner(scripts, jobs=jobs)
    from agent.graph_assign import run_assign_graph

    graph_result = run_assign_graph(graph_runner)
    return (old_result, old_events), (graph_result, graph_events)


def test_assign_parity_clean_done() -> None:
    scripts = {
        "impl": [text("DONE: 实现完成")],
        "rev": [text("DONE: 审查通过 — 很好")],
    }
    (old_r, old_e), (new_r, new_e) = _run_both(scripts, [("impl", "写代码"), ("rev", "审查")])
    assert _norm(old_e) == _norm(new_e)
    assert old_r.kind == new_r.kind == "completed"


def test_assign_parity_reject_then_revise() -> None:
    # Round 1: reviewer rejects. Round 2: reviewer approves.
    scripts = {
        "impl": [text("DONE: 初版"), text("DONE: 已按意见修改")],
        "rev": [text("REJECT: 缺少错误处理"), text("DONE: 审查通过")],
    }
    (old_r, old_e), (new_r, new_e) = _run_both(scripts, [("impl", "写代码"), ("rev", "审查")])
    assert _norm(old_e) == _norm(new_e)
    assert old_r.kind == new_r.kind
    # Both should have emitted a revision and a second round.
    assert any(e["type"] == "assign_revision" for e in new_e)
    assert any(e["type"] == "assign_round_start" and e["round"] == 2 for e in new_e)


def test_assign_parity_stop() -> None:
    scripts = {
        "impl": [text("DONE: 实现完成")],
        "rev": [text("DONE: 审查通过")],
    }
    old_runner, old_events = _make_runner(scripts, jobs=[("impl", "t"), ("rev", "r")])
    old_runner.stop_event.set()
    old_result = old_runner.run()

    graph_runner, graph_events = _make_runner(scripts, jobs=[("impl", "t"), ("rev", "r")])
    graph_runner.stop_event.set()
    from agent.graph_assign import run_assign_graph

    graph_result = run_assign_graph(graph_runner)
    assert _norm(old_events) == _norm(graph_events)
    assert old_result.kind == graph_result.kind == "stopped"


def _run_all() -> None:
    test_assign_parity_clean_done()
    test_assign_parity_reject_then_revise()
    test_assign_parity_stop()
    print("assign graph parity tests ok")


if __name__ == "__main__":
    _run_all()
