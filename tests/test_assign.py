from agent.assign import (
    format_prior_pipeline_steps,
    parse_assign_start,
    parse_reviewer_status,
    parse_task_status,
)
from agent.model_settings import ModelProfile


def _profile(name: str) -> ModelProfile:
    return ModelProfile(name, name, "https://api.example.com", "sk")


def test_parse_assign_requires_two_models() -> None:
    result = parse_assign_start("/assign a:only one", _profile)
    assert isinstance(result, str)
    assert "至少 2 个" in result


def test_parse_assign_rejects_unknown_profile() -> None:
    profiles = (
        ModelProfile("default", "deepseek-chat", "https://api.deepseek.com", "sk-a"),
        ModelProfile(
            "mimo/mimo-v2.5",
            "mimo-v2.5",
            "https://token-plan.example.com/v1",
            "sk-b",
            provider="mimo",
        ),
    )

    def resolve(name: str) -> ModelProfile:
        from agent.model_settings import find_model_profile, format_unknown_profile_error

        found = find_model_profile(name, profiles=profiles)
        if found is None:
            raise ValueError(format_unknown_profile_error(name, profiles=profiles))
        return found

    result = parse_assign_start(
        "/assign deepseek/deepseek-v4-flash:implement;;mimo/mino-v2.5:review",
        resolve,
    )
    assert isinstance(result, str)
    assert "未识别" in result
    assert "mimo/mimo-v2.5" in result


def test_parse_assign_colon_format() -> None:
    result = parse_assign_start(
        "/assign a:implement;;b:review",
        _profile,
    )
    assert len(result.jobs) == 2  # type: ignore[attr-defined]
    assert result.jobs[0].index == 0  # type: ignore[attr-defined]
    assert result.jobs[1].task == "review"  # type: ignore[attr-defined]


def test_format_prior_pipeline_steps() -> None:
    from agent.assign import AssignJob, AssignWorkerState

    jobs = (
        AssignJob("a", "implement", 0),
        AssignJob("b", "review", 1),
    )
    workers = [
        AssignWorkerState(job=jobs[0], profile=_profile("a"), completed=True, summary="done impl"),
        AssignWorkerState(job=jobs[1], profile=_profile("b")),
    ]
    text = format_prior_pipeline_steps(workers, before_index=1)
    assert "done impl" in text
    assert "步骤 1" in text


def test_parse_task_done() -> None:
    status, body = parse_task_status("DONE: 审查通过\n细节")
    assert status == "done"
    assert "细节" in body


def test_parse_reviewer_reject() -> None:
    status, body = parse_reviewer_status("REJECT: 缺少错误处理\n请补充 try/except")
    assert status == "reject"
    assert "try/except" in body


def test_parse_reviewer_approved() -> None:
    status, body = parse_reviewer_status("DONE: 审查通过\n代码质量良好")
    assert status == "approved"
    assert "代码质量" in body
