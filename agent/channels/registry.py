from __future__ import annotations

import logging

from agent.channels.base import Channel, ChannelSpec
from agent.channels.router import ChannelRouter
from agent.channels.wechat.ilink import WeChatILinkChannel
from agent.channels.wechat.official import WeChatOfficialChannel
from agent.config import ChannelConfig, Config

logger = logging.getLogger(__name__)


def list_channel_specs(channel_config: ChannelConfig) -> list[ChannelSpec]:
    specs: list[ChannelSpec] = []
    if channel_config.wechat_ilink.enabled:
        specs.append(
            ChannelSpec(
                id="wechat",
                title="微信（扫码登录）",
                description="OpenClaw 同款 iLink 协议：扫码登录 + 长轮询收消息",
            )
        )
    if channel_config.wechat_official.enabled:
        specs.append(
            ChannelSpec(
                id="wechat-official",
                title="微信公众号",
                description="服务号/订阅号 Webhook + 客服消息回复",
            )
        )
    return specs


def build_channels(
    config: Config,
    channel_config: ChannelConfig,
    router: ChannelRouter | None = None,
) -> list[Channel]:
    router = router or ChannelRouter(config)
    channels: list[Channel] = []

    if channel_config.wechat_ilink.enabled:
        channels.append(WeChatILinkChannel(channel_config.wechat_ilink, router))

    if channel_config.wechat_official.enabled:
        channels.append(WeChatOfficialChannel(channel_config.wechat_official, router))

    if not channels:
        enabled = ", ".join(channel_config.enabled) or "(无)"
        raise ValueError(
            f"没有可用的 channel。CHANNELS={enabled}，请检查 .env 中微信相关配置。"
        )

    return channels
