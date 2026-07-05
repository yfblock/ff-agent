"""LangGraph-facing LLM adapter.

The main-chat/discuss/assign graphs need to (a) pick the right model engine per
ModelProfile, and (b) stream events in the EXACT dict shape that the existing UI
consumers depend on. langchain-openai's ChatOpenAI.stream() silently drops
DeepSeek's `reasoning_content` (its delta converter only keeps content/tool_calls),
so we must NOT stream through ChatOpenAI. Instead this factory reuses the proven
openai-SDK streaming engine in agent.llm.LLMClient and caches one engine per
distinct (model, base_url, api_key, thinking_mode) — mirroring LLMClient's own
per-profile client reuse.

`stream_events()` therefore yields byte-identical dicts to LLMClient.chat_stream
(thinking_delta / content_delta / tool_call_delta / message_complete), and
`chat()` mirrors the non-streaming shape used by discuss/assign.
"""

from __future__ import annotations

from typing import Any, Iterator

from agent.config import Config
from agent.llm import LLMClient
from agent.model_settings import ModelProfile, thinking_mode_for_model


def resolve_thinking_mode(config: Config, model: str) -> bool:
    """Same rule the Agent uses: env-locked value, else inferred from model name."""
    return thinking_mode_for_model(
        model,
        locked=config.thinking_mode_locked,
        locked_value=config.thinking_mode,
    )


class ChatModelFactory:
    """Caches one LLMClient engine per (model, base_url, api_key, thinking_mode).

    A single factory is shared across a graph run; nodes call get() with the
    ModelProfile they need (main chat uses one profile; discuss/assign use one
    per participant). Cache keys match LLMClient.apply_settings semantics so a
    /model switch that only changes the model id reuses the same underlying
    openai client, while a url/key change yields a fresh one.
    """

    def __init__(self, config: Config):
        self._config = config
        self._engines: dict[tuple[str, str, str, bool], LLMClient] = {}

    def get(
        self,
        profile: ModelProfile,
        *,
        thinking_mode: bool | None = None,
    ) -> LLMClient:
        think = (
            thinking_mode
            if thinking_mode is not None
            else resolve_thinking_mode(self._config, profile.model)
        )
        key = (profile.model, profile.base_url, profile.api_key, think)
        engine = self._engines.get(key)
        if engine is None:
            engine = LLMClient(self._config)
            engine.apply_settings(
                model=profile.model,
                base_url=profile.base_url,
                api_key=profile.api_key,
                thinking_mode=think,
            )
            self._engines[key] = engine
        return engine

    def stream_events(
        self,
        profile: ModelProfile,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        thinking_mode: bool | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield the exact chat_stream event dicts for the given profile."""
        engine = self.get(profile, thinking_mode=thinking_mode)
        yield from engine.chat_stream(messages, tools=tools)

    def chat(
        self,
        profile: ModelProfile,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        thinking_mode: bool | None = None,
    ) -> Any:
        """Non-streaming call for discuss/assign participants."""
        engine = self.get(profile, thinking_mode=thinking_mode)
        return engine.chat(messages, tools=tools)
