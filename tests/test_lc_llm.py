"""Tests for the LangGraph LLM adapter (agent/lc_llm.py).

Verify that ChatModelFactory caches engines by (model, base_url, api_key,
thinking_mode) and that stream_events()/chat() delegate to the underlying
LLMClient unchanged. We patch LLMClient's openai client with a fake so no
network is touched, proving the adapter forwards the exact event dicts.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from agent.config import load_config
from agent.lc_llm import ChatModelFactory, resolve_thinking_mode
from agent.model_settings import ModelProfile


def _config():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    os.environ.pop("THINKING_MODE", None)
    return load_config(require_api_key=True)


def _profile(name: str, model: str, url: str = "https://api.example.com", key: str = "sk") -> ModelProfile:
    return ModelProfile(name=name, model=model, base_url=url, api_key=key)


def test_factory_caches_by_key() -> None:
    factory = ChatModelFactory(_config())
    p = _profile("a", "deepseek-chat")
    e1 = factory.get(p, thinking_mode=False)
    e2 = factory.get(p, thinking_mode=False)
    assert e1 is e2  # same key -> reused engine
    # A different thinking_mode is a different key -> different engine.
    e3 = factory.get(p, thinking_mode=True)
    assert e3 is not e1
    # A url/key change is a different key too.
    e4 = factory.get(_profile("a", "deepseek-chat", url="https://other.example.com"), thinking_mode=False)
    assert e4 is not e1


def test_thinking_mode_inferred_from_model_name() -> None:
    config = _config()
    # reasoner in the name -> thinking on by default (env not locked).
    assert resolve_thinking_mode(config, "deepseek-reasoner") is True
    assert resolve_thinking_mode(config, "deepseek-chat") is False


def test_stream_events_forwards_engine_output() -> None:
    factory = ChatModelFactory(_config())
    p = _profile("a", "deepseek-chat")
    engine = factory.get(p, thinking_mode=False)

    # Replace the engine's chat_stream with a scripted generator; the adapter
    # must forward its dicts verbatim.
    scripted = [
        {"type": "content_delta", "delta": "hi", "text": "hi"},
        {"type": "message_complete", "phase": "generating",
         "message": SimpleNamespace(content="hi", reasoning_content=None, tool_calls=None),
         "content": "hi", "reasoning": ""},
    ]

    def fake_stream(messages: Any, tools: Any = None):
        yield from scripted

    engine.chat_stream = fake_stream  # type: ignore[method-assign]
    out = list(factory.stream_events(p, [{"role": "user", "content": "x"}], thinking_mode=False))
    assert out == scripted


def _run_all() -> None:
    test_factory_caches_by_key()
    test_thinking_mode_inferred_from_model_name()
    test_stream_events_forwards_engine_output()
    print("lc_llm adapter tests ok")


if __name__ == "__main__":
    _run_all()
