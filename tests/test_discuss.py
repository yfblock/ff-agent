from agent.discuss import parse_discuss_start, parse_status
from agent.model_settings import ModelProfile


def _profile(name: str, model: str | None = None) -> ModelProfile:
    return ModelProfile(
        name=name,
        model=model or name,
        base_url="https://api.example.com",
        api_key="sk-test",
    )


def test_parse_discuss_start_with_pipe() -> None:
    result = parse_discuss_start(
        "/discuss a,b | 设计缓存",
        lambda name: _profile(name, {"a": "m1", "b": "m2"}.get(name, name)),
    )
    assert isinstance(result, object)
    assert result.topic == "设计缓存"  # type: ignore[attr-defined]
    assert len(result.profiles) == 2  # type: ignore[attr-defined]


def test_parse_status_consensus() -> None:
    status, body = parse_status("CONSENSUS: 统一方案\n\n详细内容")
    assert status == "consensus"
    assert "详细内容" in body


def test_parse_status_continue() -> None:
    status, body = parse_status("CONTINUE: 仍需讨论\n观点")
    assert status == "continue"
    assert "观点" in body


def test_parse_status_need_user() -> None:
    status, body = parse_status("NEED_USER: 请提供 QPS 目标\n详细说明")
    assert status == "need_user"
    assert "QPS" in body
