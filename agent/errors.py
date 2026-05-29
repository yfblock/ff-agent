from __future__ import annotations

from openai import APIConnectionError, AuthenticationError, OpenAIError, RateLimitError


def format_api_error(exc: OpenAIError) -> str:
    if isinstance(exc, AuthenticationError):
        return "API Key 无效，请检查 .env 中的 OPENAI_API_KEY。"
    if isinstance(exc, APIConnectionError):
        return f"无法连接 API 服务，请检查 OPENAI_BASE_URL。详情: {exc}"
    if isinstance(exc, RateLimitError):
        return "请求过于频繁或余额不足，请稍后再试。"
    return str(exc)
