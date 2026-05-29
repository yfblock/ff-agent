from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from pathlib import Path

from openai import OpenAIError

from agent.channels.base import ChannelAttachment, ChannelMessage, ChannelReply
from agent.channels.sessions import ChannelSessionManager
from agent.assign import AssignStart
from agent.commands import execute_command
from agent.discuss import DiscussStart
from agent.config import Config
from agent.core import Agent
from agent.errors import format_api_error
from agent.channels.utils import guess_mime

logger = logging.getLogger(__name__)

_CHANNEL_LABELS = {
    "wechat": "微信",
}


def _channel_label(channel_id: str) -> str:
    return _CHANNEL_LABELS.get(channel_id, channel_id)


def _format_channel_user_display(message: ChannelMessage, text: str) -> str:
    label = _channel_label(message.channel_id)
    lines: list[str] = []
    if text.strip():
        lines.append(f"[{label}] {text}")
    for item in message.attachments:
        if item.kind == "image":
            name = item.file_name or Path(item.path).name
            lines.append(f"[{label}] [图片: {name}]")
        elif item.kind == "file":
            name = item.file_name or Path(item.path).name
            lines.append(f"[{label}] [文件: {name}]")
        elif item.kind == "video":
            lines.append(f"[{label}] [视频: {Path(item.path).name}]")
        elif item.kind == "voice":
            lines.append(f"[{label}] [语音: {Path(item.path).name}]")
    if lines:
        return "\n".join(lines)
    return f"[{label}] [消息]"


def _channel_display_event(agent: Agent, event: dict) -> None:
    event_type = event.get("type")
    if event_type == "thinking_done":
        text = str(event.get("text") or "")
        if text.strip():
            agent.record_display("thinking", text)
    elif event_type == "tool_end":
        block = str(event.get("block") or "")
        if block.strip():
            agent.record_display("tool", block)


def _build_user_content(message: ChannelMessage) -> str | list[dict]:
    text = message.text.strip()
    images = [item for item in message.attachments if item.kind == "image"]

    if not images:
        return text

    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    else:
        parts.append({"type": "text", "text": "用户发送了图片，请描述或回答。"})

    for image in images:
        path = Path(image.path)
        if not path.is_file():
            parts.append({"type": "text", "text": f"[图片下载失败: {image.path}]"})
            continue
        mime = image.mime if image.mime.startswith("image/") else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )
    return parts


def _describe_attachments(message: ChannelMessage) -> str:
    lines: list[str] = []
    for item in message.attachments:
        if item.kind == "file":
            name = item.file_name or Path(item.path).name
            lines.append(f"[用户发送了文件: {name}，本地路径: {item.path}]")
        elif item.kind == "video":
            lines.append(f"[用户发送了视频，本地路径: {item.path}]")
        elif item.kind == "voice":
            lines.append(f"[用户发送了语音，本地路径: {item.path}]")
    return "\n".join(lines)


def _attachment_kind(path: Path, mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "voice"
    return "file"


def _attachments_from_paths(paths: list[str]) -> tuple[ChannelAttachment, ...]:
    items: list[ChannelAttachment] = []
    for raw in paths:
        path = Path(raw)
        mime = guess_mime(path)
        items.append(
            ChannelAttachment(
                kind=_attachment_kind(path, mime),  # type: ignore[arg-type]
                path=str(path),
                mime=mime,
                file_name=path.name,
            )
        )
    return tuple(items)


class ChannelRouter:
    """将 channel 消息路由到 Agent，并处理 slash 命令。"""

    def __init__(self, config: Config, sessions: ChannelSessionManager | None = None) -> None:
        self.config = config
        self.sessions = sessions or ChannelSessionManager(config)

    def handle(
        self,
        message: ChannelMessage,
        *,
        send_attachment: Callable[[str], None] | None = None,
    ) -> ChannelReply:
        agent = self.sessions.get_agent(
            message.channel_id,
            message.peer_id,
            message.account_id,
        )
        previous_channel = agent.channel_id
        agent.set_channel(message.channel_id)
        agent.set_outbound_attachment_handler(send_attachment)
        user_display = _format_channel_user_display(message, message.text.strip())
        try:
            text = message.text.strip()
            attachment_note = _describe_attachments(message)
            if attachment_note:
                text = f"{text}\n{attachment_note}".strip() if text else attachment_note

            if not text and not message.attachments:
                return ChannelReply(text="请发送文字或图片消息。")

            if text:
                command_result, should_exit = execute_command(agent, text.split("\n")[0])
                if should_exit:
                    return ChannelReply(text="渠道模式下不支持退出命令，可直接停止发消息。")
                if isinstance(command_result, DiscussStart):
                    return ChannelReply(text="多模型讨论请在 TUI 中使用 /discuss 命令。")
                if isinstance(command_result, AssignStart):
                    return ChannelReply(text="多模型流水线请在 TUI 中使用 /assign 命令。")
                if command_result is not None:
                    agent.record_display("user", user_display)
                    agent.record_display("system", command_result)
                    return ChannelReply(text=command_result)

            user_content = _build_user_content(
                ChannelMessage(
                    channel_id=message.channel_id,
                    peer_id=message.peer_id,
                    text=text,
                    message_id=message.message_id,
                    account_id=message.account_id,
                    metadata=message.metadata,
                    attachments=message.attachments,
                )
            )

            agent.record_display("user", user_display)

            def on_event(event: dict) -> None:
                _channel_display_event(agent, event)

            try:
                reply = agent.chat(
                    text or "请查看附件。",
                    on_event=on_event,
                    user_content=user_content,
                )
            except OpenAIError as exc:
                logger.exception("Agent API 错误")
                if message.attachments and "image" in str(exc).lower():
                    try:
                        reply = agent.chat(text or "请查看附件。", on_event=on_event)
                    except OpenAIError as retry_exc:
                        return ChannelReply(text=f"API 错误: {format_api_error(retry_exc)}")
                else:
                    return ChannelReply(text=f"API 错误: {format_api_error(exc)}")
            except Exception as exc:
                logger.exception("Agent 处理失败")
                return ChannelReply(text=f"处理失败: {exc}")

            agent.record_display("assistant", reply or "（无回复）")
            pending = agent.pop_pending_attachments()
            attachments = _attachments_from_paths(pending)
            return ChannelReply(text=reply or "（无回复）", attachments=attachments)
        finally:
            agent.set_outbound_attachment_handler(None)
            agent.set_channel(previous_channel)
