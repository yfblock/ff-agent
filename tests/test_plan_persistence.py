from __future__ import annotations

from agent.planner import PlanManager, PlanStatus, StepStatus


def test_plan_manager_restore() -> None:
    manager = PlanManager()
    manager.create(
        "后端登录",
        "搭建 Flask 登录",
        [{"id": "step-1", "description": "初始化项目"}],
    )
    manager.update_step("step-1", "in_progress")
    exported = manager.export_active_plan()
    assert exported is not None

    other = PlanManager()
    restored = other.restore_active_plan(exported)
    assert restored is not None
    assert restored.title == "后端登录"
    assert restored.steps[0].status == StepStatus.IN_PROGRESS


def test_completed_plan_not_restored() -> None:
    manager = PlanManager()
    manager.create("x", "y", [{"id": "s1", "description": "d"}])
    finished = manager.complete()
    other = PlanManager()
    assert other.restore_active_plan(finished.to_dict()) is None
    assert finished.status == PlanStatus.COMPLETED


if __name__ == "__main__":
    test_plan_manager_restore()
    test_completed_plan_not_restored()
    print("plan persistence tests ok")
