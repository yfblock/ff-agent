"""LangGraph graph for /discuss (multi-model discussion).

Models the discussion ROUND ORCHESTRATION as a StateGraph: the round loop, the
consensus-check conditional edge, and the NEED_USER pause. Per-participant turns
(the tool-loop in DiscussRunner._complete_with_tools) stay node-internal calls —
they have no cross-node state. The 9 discuss_* events and the status-line
(CONSENSUS/NEED_USER/CONTINUE) protocol are preserved exactly.

NEED_USER keeps the existing threading.Event pause (DiscussRunner._pause_for_user
via wait_for_user) rather than LangGraph interrupt() — no checkpointer needed and
the TUI/web/wechat pause plumbing is untouched. Round number is tracked on
runner state (a plain object) since LangGraph conditional-edge mutations don't
persist to graph channels.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.discuss import (
    DiscussResult,
    DiscussRunner,
    format_consensus_result,
    format_final_proposals,
)


class DiscussGraphState(TypedDict, total=False):
    consensus: bool     # round reached consensus -> consensus end
    paused: bool        # round paused for user -> loop back


def build_discuss_graph(runner: DiscussRunner):
    """Compile the discussion graph bound to a DiscussRunner."""

    def round_node(state: DiscussGraphState) -> DiscussGraphState:
        round_num = runner._round_num
        state["consensus"] = False
        state["paused"] = False
        if runner.stop_event.is_set():
            return state
        paused = runner._run_round(round_num)
        if paused:
            state["paused"] = True
            return state
        if runner._round_has_consensus(round_num):
            runner.state.consensus_reached = True
            runner.state.consensus_summary = runner._build_consensus_summary(round_num)
            runner._emit({
                "type": "discuss_consensus",
                "round": round_num,
                "summary": runner.state.consensus_summary,
            })
            state["consensus"] = True
        return state

    def after_round(state: DiscussGraphState) -> str:
        if runner.stop_event.is_set():
            return END
        if state.get("consensus"):
            return END
        # Both paused and completed-non-consensus rounds advance the round
        # counter — this mirrors run()'s `for round_num` loop, where a pause does
        # `continue` (moving to the next round_num), not a re-run of the same one.
        if runner._round_num >= runner.max_rounds:
            return END
        runner._round_num += 1
        return "round"

    graph = StateGraph(DiscussGraphState)
    graph.add_node("round", round_node)
    graph.add_edge(START, "round")
    graph.add_conditional_edges("round", after_round, {"round": "round", END: END})
    return graph.compile()


def run_discuss_graph(runner: DiscussRunner) -> DiscussResult:
    """Drive the discussion via LangGraph, returning the same DiscussResult."""
    runner._emit({
        "type": "discuss_start",
        "topic": runner.state.topic,
        "profiles": [p.name for p in runner.state.profiles],
        "workspace": runner.state.workspace_context.splitlines()[0]
        if runner.state.workspace_context
        else "",
    })
    runner._round_num = 1
    graph = build_discuss_graph(runner)
    graph.invoke(
        {"consensus": False, "paused": False},
        config={"recursion_limit": max(8, runner.max_rounds * 3 + 4)},
    )

    if runner.state.consensus_reached:
        text = format_consensus_result(runner.state)
        runner._emit({"type": "discuss_end", "result": text})
        return DiscussResult(kind="consensus", text=text)

    finals = runner._collect_final_proposals()
    stopped = runner.stop_event.is_set()
    text = format_final_proposals(runner.state, finals, stopped=stopped)
    runner._emit({"type": "discuss_end", "result": text})
    kind = "stopped" if stopped else "max_rounds"
    return DiscussResult(kind=kind, text=text)
