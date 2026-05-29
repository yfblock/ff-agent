from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator

from openai import OpenAI

from agent.config import Config


def serialize_assistant_message(message: Any) -> dict[str, Any]:
    """将 assistant 消息写入历史，保留 DeepSeek thinking 模式的 reasoning_content。"""
    payload: dict[str, Any] = {
        "role": "assistant",
        "content": message.content,
    }

    reasoning = getattr(message, "reasoning_content", None)
    if reasoning is None:
        extra = getattr(message, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content")
    if reasoning is not None:
        payload["reasoning_content"] = reasoning

    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": call.type,
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]

    return payload


def _delta_reasoning(delta: Any) -> str:
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        return reasoning
    extra = getattr(delta, "model_extra", None) or {}
    value = extra.get("reasoning_content")
    return value or ""


class LLMClient:
    def __init__(self, config: Config):
        self.config = config
        self.model = config.model
        self.base_url = config.base_url
        self._api_key = config.api_key
        self.thinking_mode = config.thinking_mode
        self.client = OpenAI(
            api_key=self._api_key,
            base_url=self.base_url,
        )

    def apply_settings(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        thinking_mode: bool,
    ) -> None:
        self.model = model
        self.thinking_mode = thinking_mode
        if base_url != self.base_url or api_key != self._api_key:
            self.base_url = base_url
            self._api_key = api_key
            self.client = OpenAI(api_key=api_key, base_url=base_url)

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.thinking_mode:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        return kwargs

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        kwargs = self._build_kwargs(messages, tools, stream=False)
        del kwargs["stream"]
        return self.client.chat.completions.create(**kwargs)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        stream = self.client.chat.completions.create(
            **self._build_kwargs(messages, tools, stream=True)
        )

        content = ""
        reasoning = ""
        tool_calls_acc: dict[int, dict[str, str]] = {}
        saw_reasoning = False
        saw_content = False
        saw_tool_calls = False

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            reasoning_delta = _delta_reasoning(delta)
            if reasoning_delta:
                saw_reasoning = True
                reasoning += reasoning_delta
                yield {
                    "type": "thinking_delta",
                    "delta": reasoning_delta,
                    "text": reasoning,
                }

            if delta.content:
                saw_content = True
                content += delta.content
                yield {
                    "type": "content_delta",
                    "delta": delta.content,
                    "text": content,
                }

            if delta.tool_calls:
                saw_tool_calls = True
                for tool_delta in delta.tool_calls:
                    index = tool_delta.index
                    if index not in tool_calls_acc:
                        tool_calls_acc[index] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }
                    current = tool_calls_acc[index]
                    if tool_delta.id:
                        current["id"] = tool_delta.id
                    if tool_delta.function and tool_delta.function.name:
                        current["name"] = tool_delta.function.name
                    if tool_delta.function and tool_delta.function.arguments:
                        current["arguments"] += tool_delta.function.arguments
                    yield {
                        "type": "tool_call_delta",
                        "index": index,
                        "name": current["name"],
                        "arguments": current["arguments"],
                    }

        tool_calls: list[Any] | None = None
        if tool_calls_acc:
            tool_calls = []
            for index in sorted(tool_calls_acc):
                current = tool_calls_acc[index]
                tool_calls.append(
                    SimpleNamespace(
                        id=current["id"],
                        type="function",
                        function=SimpleNamespace(
                            name=current["name"],
                            arguments=current["arguments"],
                        ),
                    )
                )

        message = SimpleNamespace(
            content=content or None,
            reasoning_content=reasoning or None,
            tool_calls=tool_calls,
        )

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
