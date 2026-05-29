from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class PlanStep:
    id: str
    description: str
    status: StepStatus = StepStatus.PENDING
    result: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "result": self.result,
        }


@dataclass
class Plan:
    id: str
    title: str
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus = PlanStatus.ACTIVE
    summary: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status.value,
            "summary": self.summary,
            "created_at": self.created_at,
            "steps": [step.to_dict() for step in self.steps],
        }

    @property
    def progress(self) -> tuple[int, int]:
        done = sum(
            1
            for step in self.steps
            if step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}
        )
        return done, len(self.steps)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        steps: list[PlanStep] = []
        for raw in data.get("steps") or []:
            if not isinstance(raw, dict):
                continue
            step_id = str(raw.get("id") or "").strip()
            description = str(raw.get("description") or "").strip()
            if not step_id or not description:
                continue
            status_raw = str(raw.get("status") or StepStatus.PENDING.value)
            try:
                status = StepStatus(status_raw)
            except ValueError:
                status = StepStatus.PENDING
            steps.append(
                PlanStep(
                    id=step_id,
                    description=description,
                    status=status,
                    result=str(raw.get("result") or ""),
                )
            )

        status_raw = str(data.get("status") or PlanStatus.ACTIVE.value)
        try:
            status = PlanStatus(status_raw)
        except ValueError:
            status = PlanStatus.ACTIVE

        return cls(
            id=str(data.get("id") or uuid4().hex[:8]),
            title=str(data.get("title") or "").strip(),
            goal=str(data.get("goal") or "").strip(),
            steps=steps,
            status=status,
            summary=str(data.get("summary") or ""),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )


PLAN_COMMAND_HELP = """计划命令:
  /plan              查看当前计划与步骤进度
  /plan clear        取消当前计划
  /plan help         显示此帮助

Agent 也可通过 create_plan / update_plan_step 等工具自动规划与跟踪执行。"""


def _status_icon(status: StepStatus) -> str:
    return {
        StepStatus.PENDING: "○",
        StepStatus.IN_PROGRESS: "→",
        StepStatus.COMPLETED: "✓",
        StepStatus.FAILED: "✗",
        StepStatus.SKIPPED: "-",
    }[status]


def format_plan_brief(plan: Plan | None) -> str:
    if plan is None or plan.status != PlanStatus.ACTIVE:
        return ""
    done, total = plan.progress
    current = next(
        (step for step in plan.steps if step.status == StepStatus.IN_PROGRESS),
        None,
    )
    if current:
        return f"计划 {done}/{total}: → {current.description[:24]}"
    return f"计划 {done}/{total}: {plan.title[:20]}"


def format_plan(plan: Plan | None) -> str:
    if plan is None:
        return "当前没有进行中的计划。"

    done, total = plan.progress
    lines = [
        f"【{plan.title}】{plan.status.value}  ({done}/{total})",
        f"目标: {plan.goal}",
    ]
    if plan.summary:
        lines.append(f"总结: {plan.summary}")

    for index, step in enumerate(plan.steps, start=1):
        icon = _status_icon(step.status)
        line = f"  {icon} {index}. [{step.id}] {step.description}"
        if step.result:
            line += f"\n      → {step.result}"
        lines.append(line)

    return "\n".join(lines)


def format_plan_prompt(plan: Plan | None) -> str:
    if plan is None or plan.status != PlanStatus.ACTIVE:
        return "（无进行中的计划）"
    return format_plan(plan)


class PlanManager:
    def __init__(self, max_steps: int = 12) -> None:
        self.max_steps = max_steps
        self._current: Plan | None = None
        self._history: list[Plan] = []

    @property
    def current(self) -> Plan | None:
        return self._current

    def create(self, title: str, goal: str, steps: list[dict[str, Any]]) -> Plan:
        if not title.strip():
            raise ValueError("计划标题不能为空")
        if not goal.strip():
            raise ValueError("计划目标不能为空")
        if not steps:
            raise ValueError("至少需要一个步骤")
        if len(steps) > self.max_steps:
            raise ValueError(f"步骤数量不能超过 {self.max_steps}")

        normalized: list[PlanStep] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(steps, start=1):
            step_id = str(raw.get("id") or f"step-{index}").strip()
            description = str(raw.get("description") or "").strip()
            if not step_id:
                raise ValueError(f"第 {index} 步缺少 id")
            if step_id in seen_ids:
                raise ValueError(f"重复的步骤 id: {step_id}")
            if not description:
                raise ValueError(f"步骤 {step_id} 缺少 description")
            seen_ids.add(step_id)
            normalized.append(PlanStep(id=step_id, description=description))

        if self._current and self._current.status == PlanStatus.ACTIVE:
            self._current.status = PlanStatus.CANCELLED
            self._history.append(self._current)

        plan = Plan(
            id=uuid4().hex[:8],
            title=title.strip(),
            goal=goal.strip(),
            steps=normalized,
        )
        self._current = plan
        return plan

    def get_step(self, step_id: str) -> PlanStep | None:
        if self._current is None:
            return None
        for step in self._current.steps:
            if step.id == step_id:
                return step
        return None

    def update_step(
        self,
        step_id: str,
        status: str,
        result: str = "",
    ) -> PlanStep:
        if self._current is None or self._current.status != PlanStatus.ACTIVE:
            raise ValueError("当前没有进行中的计划")

        step = self.get_step(step_id)
        if step is None:
            available = ", ".join(s.id for s in self._current.steps)
            raise ValueError(f"未知步骤 id: {step_id}。可用: {available}")

        try:
            step.status = StepStatus(status)
        except ValueError as exc:
            allowed = ", ".join(s.value for s in StepStatus)
            raise ValueError(f"无效状态: {status}。可用: {allowed}") from exc

        if result:
            step.result = result.strip()

        if step.status == StepStatus.IN_PROGRESS:
            for other in self._current.steps:
                if other.id != step.id and other.status == StepStatus.IN_PROGRESS:
                    other.status = StepStatus.PENDING

        return step

    def complete(self, summary: str = "") -> Plan:
        if self._current is None or self._current.status != PlanStatus.ACTIVE:
            raise ValueError("当前没有进行中的计划")

        for step in self._current.steps:
            if step.status == StepStatus.PENDING:
                step.status = StepStatus.SKIPPED
            elif step.status == StepStatus.IN_PROGRESS:
                step.status = StepStatus.COMPLETED

        self._current.status = PlanStatus.COMPLETED
        self._current.summary = summary.strip()
        finished = self._current
        self._history.append(finished)
        self._current = None
        return finished

    def cancel(self) -> Plan | None:
        if self._current is None:
            return None
        self._current.status = PlanStatus.CANCELLED
        cancelled = self._current
        self._history.append(cancelled)
        self._current = None
        return cancelled

    def export_active_plan(self) -> dict[str, Any] | None:
        if self._current is None or self._current.status != PlanStatus.ACTIVE:
            return None
        return self._current.to_dict()

    def restore_active_plan(self, data: dict[str, Any] | None) -> Plan | None:
        if not data:
            self._current = None
            return None
        try:
            plan = Plan.from_dict(data)
        except (TypeError, ValueError):
            self._current = None
            return None
        if plan.status != PlanStatus.ACTIVE or not plan.steps:
            self._current = None
            return None
        self._current = plan
        return plan

    def reset(self) -> None:
        if self._current and self._current.status == PlanStatus.ACTIVE:
            self._current.status = PlanStatus.CANCELLED
            self._history.append(self._current)
        self._current = None
