from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ChannelAttachment:
    kind: Literal["image", "file", "video", "voice"]
    path: str
    mime: str = "application/octet-stream"
    file_name: str = ""


@dataclass(frozen=True)
class ChannelMessage:
    """归一化的入站消息。"""

    channel_id: str
    peer_id: str
    text: str
    message_id: str = ""
    account_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[ChannelAttachment, ...] = ()


@dataclass(frozen=True)
class ChannelReply:
    """归一化的出站回复。"""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[ChannelAttachment, ...] = ()


@dataclass(frozen=True)
class ChannelSpec:
    id: str
    title: str
    description: str


class Channel(ABC):
    """Channel 插件契约，类似 OpenClaw 的 channel adapter。"""

    @property
    @abstractmethod
    def spec(self) -> ChannelSpec:
        raise NotImplementedError

    @abstractmethod
    def start(self) -> None:
        """启动前初始化（可选）。"""

    @abstractmethod
    def stop(self) -> None:
        """停止并释放资源（可选）。"""

    @abstractmethod
    def can_handle(self, method: str, path: str) -> bool:
        """是否由该 channel 处理此 HTTP 请求。"""

    @abstractmethod
    def handle_http(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        """处理 HTTP 请求，返回 status, headers, body。"""

    def on_message(self, message: ChannelMessage) -> ChannelReply | None:
        """非 HTTP channel 可覆写；HTTP channel 通常内部直接路由。"""
        return None
