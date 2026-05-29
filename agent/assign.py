from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from openai import OpenAIError

from agent.config import Config
from agent.discuss import build_workspace_context, parse_discuss_models
from agent.llm import LLMClient, serialize_assistant_message
from agent.model_settings import ModelProfile, thinking_mode_for_model
from agent.tool_display import format_tool_detail, format_tool_title, try_parse_tool_args
from agent.tools import ASSIGN_READ_TOOLS, ASSIGN_WRITE_TOOLS

ASSIGN_COMMAND_HELP = """流水线命令:
  /assign                              显示帮助
  /assign status                       查看进行中的流水线
  /assign stop                         停止流水线
  /assign <模型:任务;;...>             按顺序配置流水线
  /assign <模型,...> | <任务1;;任务2>  按顺序配对

示例:
  /assign deepseek/chat:实现功能;;openai/gpt-4o:审查代码
  /assign @current:实现;;openai/gpt-4o:审查

规则:
  - 模型名称须与 /model list 一致；@current 表示当前 /model 选择
  - 模型按顺序执行（流水线），后序模型审查前序产出
  - 审查通过首行写 DONE: <总结>；发现问题写 REJECT: <意见>，执行者修改后重新审查
  - 第 1 个模型负责写入（write_file），其余模型只读
  - 执行者完成后首行写 DONE: <总结>；全部审查通过或你发消息停止后结束
  - 所有模型可 read_file / list_directory / get_workspace"""


AssignResultKind = Literal["completed", "stopped", "partial"]
WorkerOutcome = Literal["done", "reject", "stopped", "error", "timeout"]
MAX_ASSIGN_TURNS = 24
MAX_ASSIGN_TOOL_STEPS = 40
MAX_ASSIGN_ROUNDS = 3


@dataclass(frozen=True)
class AssignJob:
    profile_name: str
    task: str
    index: int


@dataclass(frozen=True)
class AssignStart:
    jobs: tuple[AssignJob, ...]


@dataclass
class AssignWorkerState:
    job: AssignJob
    profile: ModelProfile | None = None
    completed: bool = False
    stopped: bool = False
    approved: bool = False
    rejected: bool = False
    summary: str = ""
    error: str = ""


@dataclass
class AssignRevision:
    round_num: int
    reviewer_index: int
    reviewer_name: str
    feedback: str


@dataclass
class AssignState:
    jobs: tuple[AssignJob, ...]
    workspace_context: str = ""
    workers: list[AssignWorkerState] = field(default_factory=list)
    current_index: int = -1
    round_num: int = 1
    max_rounds: int = MAX_ASSIGN_ROUNDS
    revisions: list[AssignRevision] = field(default_factory=list)
    stop_reason: str = ""
    pending_feedback: str = ""


@dataclass(frozen=True)
class AssignResult:
    kind: AssignResultKind
    text: str


def parse_assign_pairs(raw: str) -> list[tuple[str, str]] | str:
    text = raw.strip()
    if not text:
        return "任务不能为空"

    if ";;" in text and "|" not in text:
        pairs: list[tuple[str, str]] = []
        for part in text.split(";;"):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                return "格式: /assign model1:任务1;;model2:任务2"
            model, task = part.split(":", 1)
            model = model.strip()
            task = task.strip()
            if not model or not task:
                return "模型名与任务均不能为空"
            pairs.append((model, task))
        if not pairs:
            return "至少指定一个 model:任务"
        return pairs

    if "|" in text:
        models_part, tasks_part = text.split("|", 1)
        models = parse_discuss_models(models_part)
        tasks = [item.strip() for item in tasks_part.split(";;") if item.strip()]
        if len(models) < 1:
            return "至少指定一个模型"
        if len(models) != len(tasks):
            return f"模型数 ({len(models)}) 与任务数 ({len(tasks)}) 不一致，用 ;; 分隔任务"
        return list(zip(models, tasks))

    return ASSIGN_COMMAND_HELP


def parse_assign_start(text: str, resolve_profile: Callable[[str], ModelProfile]) -> AssignStart | str:
    rest = text[len("/assign") :].strip()
    if not rest:
        return ASSIGN_COMMAND_HELP

    parsed = parse_assign_pairs(rest)
    if isinstance(parsed, str):
        return parsed

    jobs: list[AssignJob] = []
    for index, (model_name, task) in enumerate(parsed):
        profile_name = model_name.strip()
        if not profile_name:
            return "模型名不能为空"
        try:
            resolve_profile(profile_name)
        except ValueError as exc:
            return str(exc)
        jobs.append(AssignJob(profile_name=profile_name, task=task, index=index))

    if len(jobs) < 2:
        return "流水线至少需要 2 个模型（1 个执行 + 1 个审查）"
    return AssignStart(jobs=tuple(jobs))


def _worker_profile(worker: AssignWorkerState) -> ModelProfile:
    if worker.profile is None:
        raise RuntimeError("流水线步骤尚未解析模型配置")
    return worker.profile


def parse_task_status(content: str) -> tuple[str, str]:
    text = (content or "").strip()
    if not text:
        return "working", ""
    first_line = text.splitlines()[0].strip()
    upper = first_line.upper()
    body = text[len(first_line) :].lstrip("\n").strip()
    if upper.startswith("DONE:"):
        summary = first_line.split(":", 1)[1].strip()
        return "done", body or summary
    return "working", text


def parse_reviewer_status(content: str) -> tuple[str, str]:
    text = (content or "").strip()
    if not text:
        return "working", ""
    first_line = text.splitlines()[0].strip()
    upper = first_line.upper()
    body = text[len(first_line) :].lstrip("\n").strip()
    if upper.startswith("REJECT:") or upper.startswith("ISSUES:"):
        summary = first_line.split(":", 1)[1].strip()
        return "reject", body or summary or text
    if upper.startswith("DONE:"):
        summary = first_line.split(":", 1)[1].strip()
        return "approved", body or summary
    return "working", text


def format_prior_pipeline_steps(workers: list[AssignWorkerState], before_index: int) -> str:
    lines: list[str] = []
    for worker in workers:
        if worker.job.index >= before_index:
            break
        step = worker.job.index + 1
        profile_name = (
            _worker_profile(worker).name if worker.profile is not None else worker.job.profile_name
        )
        header = f"步骤 {step} [{profile_name}] 任务: {worker.job.task}"
        if worker.completed:
            lines.extend([header, f"总结:\n{worker.summary.strip() or '（无）'}", ""])
        elif worker.approved:
            lines.extend([header, f"审查通过:\n{worker.summary.strip() or '（无）'}", ""])
        elif worker.rejected:
            lines.extend([header, f"要求修改:\n{worker.summary.strip() or '（无）'}", ""])
        elif worker.stopped:
            lines.extend([header, "状态: 已停止", ""])
        elif worker.error:
            lines.extend([header, f"状态: 失败 — {worker.error}", ""])
        else:
            lines.extend([header, "状态: 未完成", ""])
    return "\n".join(lines).strip() or "（无前序步骤）"


def format_assign_status(state: AssignState) -> str:
    total = len(state.workers)
    lines = [
        "流水线进行中",
        f"轮次: {state.round_num}/{state.max_rounds}",
        f"工作区: {state.workspace_context.splitlines()[0] if state.workspace_context else '未知'}",
        "",
    ]
    for worker in state.workers:
        job = worker.job
        step = job.index + 1
        profile_name = (
            _worker_profile(worker).name if worker.profile is not None else job.profile_name
        )
        if worker.approved:
            status = "审查通过"
        elif worker.rejected:
            status = "要求修改"
        elif worker.completed:
            status = "已完成"
        elif worker.stopped:
            status = "已停止"
        elif worker.error:
            status = f"失败: {worker.error}"
        elif job.index == state.current_index:
            status = "执行中"
        else:
            status = "等待中"
        role = "执行/写入" if job.index == 0 else "审查"
        marker = "  ← 当前" if job.index == state.current_index else ""
        lines.append(f"  步骤 {step}/{total} [{profile_name}] ({role}): {status}{marker}")
        lines.append(f"    任务: {job.task}")
    return "\n".join(lines)


def format_assign_result(state: AssignState, *, stopped: bool) -> str:
    done = sum(1 for w in state.workers if w.approved or (w.completed and w.job.index == 0))
    total = len(state.workers)
    reviewers = [w for w in state.workers if w.job.index > 0]
    all_approved = reviewers and all(w.approved for w in reviewers)
    if stopped:
        header = f"流水线已停止 (第 {state.round_num}/{state.max_rounds} 轮)"
    elif all_approved:
        header = f"流水线已全部完成 (第 {state.round_num}/{state.max_rounds} 轮，审查通过)"
    elif state.round_num >= state.max_rounds and state.pending_feedback.strip():
        header = f"流水线结束：已达最大轮次 ({state.max_rounds})，仍有未解决审查意见"
    else:
        header = f"流水线结束 (第 {state.round_num}/{state.max_rounds} 轮)"

    parts = [header, ""]
    if state.stop_reason.strip():
        parts.extend([f"停止说明: {state.stop_reason.strip()}", ""])
    if state.revisions:
        parts.append("审查反馈记录:")
        for item in state.revisions:
            parts.extend(
                [
                    f"- 第 {item.round_num} 轮 · [{item.reviewer_name}]:",
                    item.feedback.strip() or "（无）",
                    "",
                ]
            )
    for worker in state.workers:
        job = worker.job
        step = job.index + 1
        profile_name = (
            _worker_profile(worker).name if worker.profile is not None else job.profile_name
        )
        role = "执行/写入" if job.index == 0 else "审查"
        if worker.approved:
            label = "审查通过"
        elif worker.rejected:
            label = "要求修改"
        elif worker.completed:
            label = "完成"
        elif worker.stopped:
            label = "已停止"
        elif worker.error:
            label = f"失败: {worker.error}"
        else:
            label = "未执行"
        parts.extend(
            [
                f"=== 步骤 {step} · {profile_name} ({role}) · {label} ===",
                f"任务: {job.task}",
                worker.summary.strip() or worker.error or "（无总结）",
                "",
            ]
        )
    return "\n".join(parts).strip()


class AssignRunner:
    def __init__(
        self,
        *,
        config: Config,
        state: AssignState,
        stop_event: threading.Event,
        handle_tool: Callable[[int, str, str], str],
        resolve_profile: Callable[[str], ModelProfile],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        profile_warning: Callable[[str, ModelProfile], str | None] | None = None,
        max_turns: int = MAX_ASSIGN_TURNS,
        max_tool_steps: int = MAX_ASSIGN_TOOL_STEPS,
        max_rounds: int = MAX_ASSIGN_ROUNDS,
    ) -> None:
        self.config = config
        self.state = state
        self.stop_event = stop_event
        self.handle_tool = handle_tool
        self.resolve_profile = resolve_profile
        self.profile_warning = profile_warning
        self.on_event = on_event
        self.max_turns = max(1, max_turns)
        self.max_tool_steps = max(1, max_tool_steps)
        self.max_rounds = max(1, max_rounds)
        self.state.max_rounds = self.max_rounds
        self._clients: dict[str, LLMClient] = {}

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.on_event:
            self.on_event(payload)

    def _client_key(self, profile: ModelProfile) -> str:
        return f"{profile.name}\0{profile.model}\0{profile.base_url}\0{profile.api_key}"

    def _client_for(self, profile: ModelProfile) -> LLMClient:
        key = self._client_key(profile)
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        client = LLMClient(self.config)
        client.apply_settings(
            model=profile.model,
            base_url=profile.base_url,
            api_key=profile.api_key,
            thinking_mode=thinking_mode_for_model(
                profile.model,
                locked=self.config.thinking_mode_locked,
                locked_value=self.config.thinking_mode,
            ),
        )
        self._clients[key] = client
        return client

    def _tools_for(self, index: int) -> list[dict[str, Any]]:
        return ASSIGN_WRITE_TOOLS if index == 0 else ASSIGN_READ_TOOLS

    def _reset_worker(self, worker: AssignWorkerState) -> None:
        worker.completed = False
        worker.stopped = False
        worker.approved = False
        worker.rejected = False
        worker.summary = ""
        worker.error = ""

    def _worker_system(
        self,
        worker: AssignWorkerState,
        *,
        round_num: int = 1,
        is_revision: bool = False,
    ) -> str:
        job = worker.job
        profile = _worker_profile(worker)
        step = job.index + 1
        total = len(self.state.workers)
        prior = format_prior_pipeline_steps(self.state.workers, job.index)
        read_tools = "read_file / list_directory / get_workspace"
        round_note = f"当前为第 {round_num}/{self.max_rounds} 轮。"

        if job.index == 0:
            revision_note = ""
            if is_revision:
                revision_note = (
                    "审查方提出了修改意见，请根据意见修改工作区代码后再次提交。\n"
                )
            role_block = (
                f"你是流水线第 1 步（共 {total} 步）的执行者 [{profile.name}]。\n"
                f"{round_note}{revision_note}"
                "你负责完成主要实现，并拥有唯一 write_file 写入权限。\n"
                f"后续 {total - 1} 个模型将顺序审查你的工作区产出。"
            )
            perm = f"可用工具: write_file + {read_tools}"
            rules = (
                "1. 读取相关文件后完成实现，用 write_file 写入改动。\n"
                "2. 完成后首行写 DONE: <总结>，说明改动文件与要点。\n"
                "3. 收到审查 REJECT 意见后，按意见修改并再次 DONE。"
            )
        else:
            role_block = (
                f"你是流水线第 {step} 步（共 {total} 步）的审查者 [{profile.name}]。\n"
                f"{round_note}"
                "前序步骤已完成，你需要读取工作区实际改动，审查前序产出是否满足要求。\n"
                f"你的任务通常包括: {job.task}"
            )
            perm = f"仅有读取权限（{read_tools}），不可 write_file"
            rules = (
                "1. 用工具读取前序步骤涉及的文件，核实实际改动与总结是否一致。\n"
                "2. 审查通过：首行写 DONE: 审查通过 — <总结>。\n"
                "3. 发现问题需修改：首行写 REJECT: <简要结论>，正文列出具体问题和修改建议。\n"
                "4. 不要自行改文件，只给出审查意见。"
            )

        upcoming = []
        for other in self.state.workers:
            if other.job.index <= job.index:
                continue
            other_profile = (
                _worker_profile(other).name
                if other.profile is not None
                else other.job.profile_name
            )
            upcoming.append(f"- 步骤 {other.job.index + 1} [{other_profile}]: {other.job.task}")
        upcoming_block = "\n".join(upcoming) if upcoming else "（你是最后一步）"

        return (
            f"{role_block}\n\n"
            f"当前工作区:\n{self.state.workspace_context}\n\n"
            f"前序步骤:\n{prior}\n\n"
            f"后续步骤:\n{upcoming_block}\n\n"
            f"权限: {perm}\n\n"
            f"你的任务: {job.task}\n\n"
            "规则:\n"
            f"{rules}\n"
            "5. 使用中文。"
        )

    def _initial_user_prompt(
        self,
        worker: AssignWorkerState,
        *,
        round_num: int = 1,
        feedback: str = "",
        is_revision: bool = False,
    ) -> str:
        job = worker.job
        step = job.index + 1
        if job.index == 0:
            if is_revision and feedback.strip():
                return (
                    f"第 {round_num} 轮修改：审查方提出了以下意见，请读取工作区并按意见修改。\n"
                    f"完成后首行写 DONE: <总结>\n\n"
                    f"审查意见:\n{feedback.strip()}\n\n"
                    f"原始任务: {job.task}"
                )
            return (
                f"请开始流水线第 1 步（第 {round_num} 轮）。完成后首行写 DONE: <总结>\n\n"
                f"任务: {job.task}"
            )
        prior = format_prior_pipeline_steps(self.state.workers, job.index)
        return (
            f"请开始流水线第 {step} 步（审查，第 {round_num} 轮）。"
            "先读取工作区验证前序产出，再完成你的任务。\n"
            f"通过写 DONE: 审查通过 — <总结>；有问题写 REJECT: <意见>\n\n"
            f"前序步骤:\n{prior}\n\n"
            f"你的任务: {job.task}"
        )

    def _complete_with_tools(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        *,
        worker: AssignWorkerState,
    ) -> str:
        tools = self._tools_for(worker.job.index)
        for _ in range(self.max_tool_steps):
            if self.stop_event.is_set():
                break
            response = client.chat(messages, tools=tools)
            message = response.choices[0].message
            if not message.tool_calls:
                return (message.content or "").strip()
            messages.append(serialize_assistant_message(message))
            for call in message.tool_calls:
                name = call.function.name
                arguments = call.function.arguments or "{}"
                args = try_parse_tool_args(arguments)
                self._emit(
                    {
                        "type": "assign_tool",
                        "profile": _worker_profile(worker).name,
                        "index": worker.job.index,
                        "step": worker.job.index + 1,
                        "name": name,
                        "title": format_tool_title(name, args),
                        "detail": format_tool_detail(name, args),
                    }
                )
                result = self.handle_tool(worker.job.index, name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    }
                )
        return ""

    def _run_worker(
        self,
        worker: AssignWorkerState,
        *,
        round_num: int = 1,
        feedback: str = "",
        is_revision: bool = False,
    ) -> WorkerOutcome:
        job = worker.job
        profile = self.resolve_profile(job.profile_name)
        worker.profile = profile
        step = job.index + 1
        total = len(self.state.workers)
        self._emit(
            {
                "type": "assign_worker_start",
                "profile": profile.name,
                "model": profile.model,
                "base_url": profile.base_url,
                "index": job.index,
                "step": step,
                "total": total,
                "round": round_num,
                "task": job.task,
                "can_write": job.index == 0,
                "role": "executor" if job.index == 0 else "reviewer",
                "is_revision": is_revision,
            }
        )
        client = self._client_for(profile)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._worker_system(
                    worker,
                    round_num=round_num,
                    is_revision=is_revision,
                ),
            },
            {
                "role": "user",
                "content": self._initial_user_prompt(
                    worker,
                    round_num=round_num,
                    feedback=feedback,
                    is_revision=is_revision,
                ),
            },
        ]
        try:
            for turn in range(1, self.max_turns + 1):
                if self.stop_event.is_set():
                    worker.stopped = True
                    worker.summary = "用户已请求停止"
                    return "stopped"
                content = self._complete_with_tools(client, messages, worker=worker)
                if self.stop_event.is_set():
                    worker.stopped = True
                    worker.summary = "用户已请求停止"
                    return "stopped"
                if not content:
                    continue
                if job.index == 0:
                    status, body = parse_task_status(content)
                else:
                    status, body = parse_reviewer_status(content)
                self._emit(
                    {
                        "type": "assign_progress",
                        "profile": profile.name,
                        "index": job.index,
                        "step": step,
                        "round": round_num,
                        "turn": turn,
                        "status": status,
                        "content": body or content,
                    }
                )
                if status in {"done", "approved"}:
                    worker.completed = True
                    worker.approved = job.index > 0
                    worker.summary = body or content
                    self._emit(
                        {
                            "type": "assign_worker_done",
                            "profile": profile.name,
                            "index": job.index,
                            "step": step,
                            "round": round_num,
                            "outcome": "approved" if job.index > 0 else "done",
                            "summary": worker.summary,
                        }
                    )
                    return "done"
                if status == "reject":
                    worker.completed = True
                    worker.rejected = True
                    worker.summary = body or content
                    self._emit(
                        {
                            "type": "assign_worker_done",
                            "profile": profile.name,
                            "index": job.index,
                            "step": step,
                            "round": round_num,
                            "outcome": "reject",
                            "summary": worker.summary,
                        }
                    )
                    return "reject"
                messages.append({"role": "assistant", "content": content})
                if job.index == 0:
                    follow_up = "继续执行。完成后首行写 DONE: <总结>。"
                else:
                    follow_up = (
                        "继续审查。通过写 DONE: 审查通过 — <总结>；"
                        "有问题写 REJECT: <意见>。"
                    )
                messages.append({"role": "user", "content": follow_up})
            worker.error = f"超过最大轮次 ({self.max_turns}) 仍未完成"
            return "timeout"
        except OpenAIError as exc:
            worker.error = str(exc)
            return "error"
        except Exception as exc:
            worker.error = str(exc)
            return "error"

    def run(self) -> AssignResult:
        warnings: list[str] = []
        workers: list[AssignWorkerState] = []
        for job in self.state.jobs:
            profile = self.resolve_profile(job.profile_name)
            if self.profile_warning is not None:
                warning = self.profile_warning(job.profile_name, profile)
                if warning:
                    warnings.append(warning)
            workers.append(AssignWorkerState(job=job, profile=profile))
        self.state.workers = workers
        total = len(self.state.jobs)
        self._emit(
            {
                "type": "assign_start",
                "mode": "pipeline",
                "warnings": warnings,
                "jobs": [
                    {
                        "profile": worker.profile.name if worker.profile else job.profile_name,
                        "model": worker.profile.model if worker.profile else "",
                        "base_url": worker.profile.base_url if worker.profile else "",
                        "task": job.task,
                        "step": job.index + 1,
                        "can_write": job.index == 0,
                        "role": "executor" if job.index == 0 else "reviewer",
                    }
                    for job, worker in zip(self.state.jobs, workers, strict=True)
                ],
                "workspace": self.state.workspace_context.splitlines()[0]
                if self.state.workspace_context
                else "",
            }
        )

        self._run_pipeline()
        return self._finalize_result()

    def _run_pipeline(self) -> None:
        round_num = 1
        feedback = ""
        total = len(self.state.jobs)
        executor = self.state.workers[0]
        reviewers = self.state.workers[1:]

        while round_num <= self.max_rounds:
            if self.stop_event.is_set():
                break

            self.state.round_num = round_num
            is_revision = round_num > 1
            if is_revision:
                for worker in self.state.workers:
                    self._reset_worker(worker)

            self._emit(
                {
                    "type": "assign_round_start",
                    "round": round_num,
                    "max_rounds": self.max_rounds,
                    "is_revision": is_revision,
                }
            )

            self.state.current_index = 0
            self._emit(
                {
                    "type": "assign_pipeline_step",
                    "step": 1,
                    "total": total,
                    "profile": _worker_profile(executor).name,
                    "base_url": _worker_profile(executor).base_url,
                    "round": round_num,
                }
            )
            outcome = self._run_worker(
                executor,
                round_num=round_num,
                feedback=feedback,
                is_revision=is_revision,
            )
            if outcome in {"stopped", "error", "timeout"}:
                break
            if outcome != "done":
                break

            reject_feedback = ""
            rejected = False
            for reviewer in reviewers:
                if self.stop_event.is_set():
                    reviewer.stopped = True
                    reviewer.summary = "用户已请求停止"
                    break

                self.state.current_index = reviewer.job.index
                self._emit(
                    {
                        "type": "assign_pipeline_step",
                        "step": reviewer.job.index + 1,
                        "total": total,
                        "profile": _worker_profile(reviewer).name,
                        "base_url": _worker_profile(reviewer).base_url,
                        "round": round_num,
                    }
                )
                outcome = self._run_worker(reviewer, round_num=round_num)
                if outcome == "reject":
                    rejected = True
                    reject_feedback = reviewer.summary
                    self.state.pending_feedback = reject_feedback
                    self.state.revisions.append(
                        AssignRevision(
                            round_num=round_num,
                            reviewer_index=reviewer.job.index,
                            reviewer_name=_worker_profile(reviewer).name,
                            feedback=reject_feedback,
                        )
                    )
                    self._emit(
                        {
                            "type": "assign_revision",
                            "round": round_num,
                            "from_step": reviewer.job.index + 1,
                            "profile": _worker_profile(reviewer).name,
                            "feedback": reject_feedback,
                        }
                    )
                    break
                if outcome != "done":
                    rejected = True
                    break

            if self.stop_event.is_set():
                break
            if not rejected:
                self.state.pending_feedback = ""
                break

            if round_num >= self.max_rounds:
                break

            feedback = reject_feedback
            round_num += 1

        self.state.current_index = -1

    def _finalize_result(self) -> AssignResult:
        stopped = self.stop_event.is_set()
        reviewers = [w for w in self.state.workers if w.job.index > 0]
        all_approved = bool(reviewers) and all(w.approved for w in reviewers)
        if all_approved and not stopped:
            kind: AssignResultKind = "completed"
        elif stopped:
            kind = "stopped"
        else:
            kind = "partial"
        text = format_assign_result(self.state, stopped=stopped)
        self._emit({"type": "assign_end", "result": text, "kind": kind})
        return AssignResult(kind=kind, text=text)
