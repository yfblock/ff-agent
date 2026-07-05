"""Test doubles for driving Agent.chat() deterministically.

FakeLLM replays scripted responses as the exact event dict sequence that
agent.llm.LLMClient.chat_stream() yields, so golden tests can assert the
on_event contract without hitting a real model. It also supports the
non-streaming chat() shape used by discuss/assign.

The scripted-response format (one per model turn):

    text("hello")                      -> a plain assistant reply
    text("答案", reasoning="想一下")    -> reply preceded by reasoning tokens
    tools([("read_file", {"path": "a"})])  -> one assistant turn calling tools

A FakeLLM is constructed with a list of these and consumed one per model
turn; when the script is exhausted it raises, which surfaces loops that call
the model more often than expected.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterator


def text(content: str, *, reasoning: str = "") -> dict[str, Any]:
    """A scripted assistant turn that returns a final text reply."""
    return {"content": content, "reasoning": reasoning, "tool_calls": []}


def tools(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """A scripted assistant turn that emits tool calls.

    ``calls`` is a list of (tool_name, arguments_dict).
    """
    return {"content": "", "reasoning": "", "tool_calls": calls}


class FakeLLM:
    """Drop-in replacement for Agent.llm.

    Replays ``responses`` (built via text()/tools()) one per model turn,
    reproducing chat_stream()'s event dicts and message_complete SimpleNamespace
    exactly so downstream serialization/event code is unchanged.
    """

    def __init__(self, responses: list[dict[str, Any]]):
        self._responses = list(responses)
        self._turn = 0
        # Mirror the public attributes callers read off LLMClient.
        self.model = "fake-model"
        self.base_url = "https://fake.example.com"
        self.thinking_mode = False
        self.calls: list[list[dict[str, Any]]] = []

    def apply_settings(self, **kwargs: Any) -> None:  # pragma: no cover - trivial
        if "model" in kwargs and kwargs["model"]:
            self.model = kwargs["model"]
        if kwargs.get("base_url"):
            self.base_url = kwargs["base_url"]
        if "thinking_mode" in kwargs:
            self.thinking_mode = bool(kwargs["thinking_mode"])

    def _next(self) -> dict[str, Any]:
        if self._turn >= len(self._responses):
            raise AssertionError(
                f"FakeLLM ran out of scripted responses at turn {self._turn + 1}; "
                "the loop called the model more times than expected."
            )
        response = self._responses[self._turn]
        self._turn += 1
        return response

    def _build_message(self, response: dict[str, Any]) -> SimpleNamespace:
        content = response.get("content") or ""
        reasoning = response.get("reasoning") or ""
        tool_specs = response.get("tool_calls") or []
        tool_calls: list[Any] | None = None
        if tool_specs:
            tool_calls = []
            for idx, (name, args) in enumerate(tool_specs):
                tool_calls.append(
                    SimpleNamespace(
                        id=f"call_{self._turn}_{idx}",
                        type="function",
                        function=SimpleNamespace(
                            name=name,
                            arguments=json.dumps(args, ensure_ascii=False),
                        ),
                    )
                )
        return SimpleNamespace(
            content=content or None,
            reasoning_content=reasoning or None,
            tool_calls=tool_calls,
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        self.calls.append([dict(m) for m in messages])
        response = self._next()
        content = response.get("content") or ""
        reasoning = response.get("reasoning") or ""
        tool_specs = response.get("tool_calls") or []

        saw_reasoning = bool(reasoning)
        saw_content = bool(content)
        saw_tool_calls = bool(tool_specs)

        # Emit reasoning as a single incremental delta (accumulated == delta here).
        if reasoning:
            yield {"type": "thinking_delta", "delta": reasoning, "text": reasoning}
        if content:
            yield {"type": "content_delta", "delta": content, "text": content}
        for idx, (name, args) in enumerate(tool_specs):
            raw = json.dumps(args, ensure_ascii=False)
            yield {
                "type": "tool_call_delta",
                "index": idx,
                "name": name,
                "arguments": raw,
            }

        message = self._build_message(response)
        if saw_reasoning and not saw_content and not saw_tool_calls:
            phase = "thinking"
        elif saw_tool_calls and not saw_content:
            phase = "tool_planning"
        elif saw_content:
            phase = "generating"
        else:
            phase = "waiting"

        yield {
            "type": "message_complete",
            "phase": phase,
            "message": message,
            "content": content,
            "reasoning": reasoning,
        }

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Non-streaming shape used by discuss/assign runners."""
        self.calls.append([dict(m) for m in messages])
        response = self._next()
        message = self._build_message(response)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])
