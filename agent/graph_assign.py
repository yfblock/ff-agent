"""LangGraph pipeline graph for /assign.

Models the assign PIPELINE ORCHESTRATION as a StateGraph — the part that is
genuinely a graph: executor step, sequential reviewer steps, and the reject ->
revision back-edge across rounds. Single-worker execution (the internal
tool-loop + turn-loop in AssignRunner._run_worker) is NOT split into nodes: it
has no cross-node state or branching, so it stays a node-internal call. This
keeps the 10 assign_* events and reject/revision semantics identical to
AssignRunner._run_pipeline while making the round/revision control flow explicit.

Reuses the existing AssignRunner for all worker execution, profile resolution,
client caching, prompts, parsers, and event emission — the graph only decides
the flow between steps.
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.assign import AssignResult, AssignRunner, _worker_profile


class AssignGraphState(TypedDict, total=False):
    # Round + feedback are tracked on runner.state (persists reliably across
    # nodes); graph state only carries per-visit routing flags read by edges.
    aborted: bool          # executor stopped/errored -> finalize
    rejected: bool         # a reviewer rejected -> maybe revise
    reject_feedback: str


def build_assign_graph(runner: AssignRunner):
    """Compile the pipeline graph bound to a prepared AssignRunner."""

    def executor_node(state: AssignGraphState) -> AssignGraphState:
        # Round + feedback live on runner.state (a plain object that persists
        # reliably across nodes), not graph state — LangGraph's LastValue channel
        # does not merge mutations made in conditional-edge functions, and only
        # merges node returns, so runner.state is the robust source of truth.
        round_num = runner.state.round_num
        # Match _run_pipeline: a stop requested before the round starts means
        # the round body never runs (no round_start / step events emitted).
        if runner.stop_event.is_set():
            state["aborted"] = True
            return state
        is_revision = round_num > 1
        if is_revision:
            for worker in runner.state.workers:
                runner._reset_worker(worker)

        runner._emit({
            "type": "assign_round_start",
            "round": round_num,
            "max_rounds": runner.max_rounds,
            "is_revision": is_revision,
        })

        executor = runner.state.workers[0]
        total = len(runner.state.jobs)
        runner.state.current_index = 0
        runner._emit({
            "type": "assign_pipeline_step",
            "step": 1,
            "total": total,
            "profile": _worker_profile(executor).name,
            "base_url": _worker_profile(executor).base_url,
            "round": round_num,
        })
        outcome = runner._run_worker(
            executor,
            round_num=round_num,
            feedback=runner.state.pending_feedback,
            is_revision=is_revision,
        )
        state["aborted"] = outcome != "done"
        return state

    def review_node(state: AssignGraphState) -> AssignGraphState:
        round_num = runner.state.round_num
        total = len(runner.state.jobs)
        reviewers = runner.state.workers[1:]
        state["rejected"] = False
        state["reject_feedback"] = ""
        for reviewer in reviewers:
            if runner.stop_event.is_set():
                reviewer.stopped = True
                reviewer.summary = "用户已请求停止"
                state["aborted"] = True
                return state
            runner.state.current_index = reviewer.job.index
            runner._emit({
                "type": "assign_pipeline_step",
                "step": reviewer.job.index + 1,
                "total": total,
                "profile": _worker_profile(reviewer).name,
                "base_url": _worker_profile(reviewer).base_url,
                "round": round_num,
            })
            outcome = runner._run_worker(reviewer, round_num=round_num)
            if outcome == "reject":
                state["rejected"] = True
                state["reject_feedback"] = reviewer.summary
                runner.state.pending_feedback = reviewer.summary
                from agent.assign import AssignRevision

                runner.state.revisions.append(
                    AssignRevision(
                        round_num=round_num,
                        reviewer_index=reviewer.job.index,
                        reviewer_name=_worker_profile(reviewer).name,
                        feedback=reviewer.summary,
                    )
                )
                runner._emit({
                    "type": "assign_revision",
                    "round": round_num,
                    "from_step": reviewer.job.index + 1,
                    "profile": _worker_profile(reviewer).name,
                    "feedback": reviewer.summary,
                })
                return state
            if outcome != "done":
                state["aborted"] = True
                return state
        return state

    def after_executor(state: AssignGraphState) -> str:
        if state.get("aborted") or runner.stop_event.is_set():
            return END
        return "review"

    def after_review(state: AssignGraphState) -> str:
        if runner.stop_event.is_set() or state.get("aborted"):
            return END
        if not state.get("rejected"):
            runner.state.pending_feedback = ""
            return END
        if runner.state.round_num >= runner.max_rounds:
            return END
        # Revision: bump the round on runner.state (persists across nodes) and
        # loop back to the executor, which reads runner.state.round_num.
        runner.state.round_num += 1
        return "executor"

    graph = StateGraph(AssignGraphState)
    graph.add_node("executor", executor_node)
    graph.add_node("review", review_node)
    graph.add_edge(START, "executor")
    graph.add_conditional_edges("executor", after_executor, {"review": "review", END: END})
    graph.add_conditional_edges("review", after_review, {"executor": "executor", END: END})
    return graph.compile()


def run_assign_graph(runner: AssignRunner) -> AssignResult:
    """Drive the assign pipeline via LangGraph, returning the same AssignResult."""
    runner._prepare_workers()
    runner.state.round_num = 1
    graph = build_assign_graph(runner)
    graph.invoke(
        {"aborted": False, "rejected": False},
        config={"recursion_limit": max(8, runner.max_rounds * 4 + 4)},
    )
    runner.state.current_index = -1
    return runner._finalize_result()
