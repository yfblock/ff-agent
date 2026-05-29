from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from agent.channels.registry import build_channels
from agent.channels.router import ChannelRouter
from agent.channels.sessions import ChannelSessionManager
from agent.config import ChannelConfig, Config, load_channel_config, load_config
from agent.session_hub import SessionHub

if TYPE_CHECKING:
    from agent.channels.base import Channel

logger = logging.getLogger(__name__)


class ChannelGateway:
    def __init__(
        self,
        config: Config,
        channel_config: ChannelConfig,
        *,
        router: ChannelRouter | None = None,
        hub: SessionHub | None = None,
    ) -> None:
        self.config = config
        self.channel_config = channel_config
        self.hub = hub
        if router is None:
            sessions = ChannelSessionManager(config, hub=hub)
            router = ChannelRouter(config, sessions=sessions)
        self.router = router
        self.channels: list[Channel] = build_channels(config, channel_config, router)
        self._channel_by_path: dict[str, Channel] = {}
        if channel_config.wechat_official.enabled:
            from agent.channels.wechat.official import WeChatOfficialChannel

            path = channel_config.wechat_official.webhook_path.rstrip("/")
            for channel in self.channels:
                if isinstance(channel, WeChatOfficialChannel):
                    self._channel_by_path[path] = channel

    def start(self) -> None:
        for channel in self.channels:
            channel.start()
            logger.info("Channel 已启动: %s (%s)", channel.spec.title, channel.spec.id)

    def stop(self) -> None:
        for channel in self.channels:
            channel.stop()
            logger.info("Channel 已停止: %s", channel.spec.id)

    def needs_http(self) -> bool:
        return self.channel_config.wechat_official.enabled

    def dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        normalized = path.rstrip("/") or "/"
        channel = self._channel_by_path.get(normalized)
        if channel is None:
            for item in self.channels:
                if item.can_handle(method, normalized):
                    channel = item
                    break
        if channel is None:
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"channel not found"
        return channel.handle_http(method, normalized, query, headers, body)


def run_channel_gateway(
    env_path: str | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    hub: SessionHub | None = None,
    block: bool = True,
) -> ChannelGateway | None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    config = load_config(env_path)
    channel_config = load_channel_config(env_path)
    hub = hub or SessionHub(config)
    gateway = ChannelGateway(config, channel_config, hub=hub)
    gateway.start()

    if not block:
        return gateway

    if gateway.needs_http():
        bind_host = host or channel_config.gateway_host
        bind_port = port or channel_config.gateway_port

        class Handler(BaseHTTPRequestHandler):
            gateway_ref = gateway

            def do_GET(self) -> None:
                self._dispatch()

            def do_POST(self) -> None:
                self._dispatch()

            def _dispatch(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k: v for k, v in self.headers.items()}
                status, resp_headers, payload = self.gateway_ref.dispatch(
                    self.command,
                    parsed.path,
                    query,
                    headers,
                    body,
                )
                self.send_response(status)
                for key, value in resp_headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                logger.info("%s - %s", self.address_string(), format % args)

        server = ThreadingHTTPServer((bind_host, bind_port), Handler)
        logger.info("Channel Gateway HTTP 监听 http://%s:%s", bind_host, bind_port)
        if channel_config.wechat_official.enabled:
            path = channel_config.wechat_official.webhook_path
            logger.info("  微信公众号 Webhook: http://%s:%s%s", bind_host, bind_port, path)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("正在停止 Channel Gateway…")
        finally:
            server.server_close()
            gateway.stop()
        return None

    logger.info("微信 iLink 模式运行中（无需 HTTP Webhook）。按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("正在停止 Channel Gateway…")
    finally:
        gateway.stop()
    return None


def start_channel_gateway_background(
    env_path: str | None,
    hub: SessionHub,
) -> tuple[ChannelGateway | None, threading.Thread | None]:
    """在后台线程启动 Gateway，与 TUI 共用 SessionHub。"""
    try:
        channel_config = load_channel_config(env_path)
    except ValueError:
        return None, None

    if not channel_config.enabled:
        return None, None

    config = hub.config
    sessions = ChannelSessionManager(config, hub=hub)
    router = ChannelRouter(config, sessions=sessions)
    gateway = ChannelGateway(config, channel_config, router=router, hub=hub)

    def _run() -> None:
        try:
            gateway.start()
            if gateway.needs_http():
                logger.warning("HTTP Gateway 暂不支持后台模式，请单独运行 --channel-gateway")
                gateway.stop()
                return
            while True:
                time.sleep(3600)
        except Exception:
            logger.exception("Channel Gateway 后台线程异常退出")
            gateway.stop()

    thread = threading.Thread(target=_run, name="channel-gateway", daemon=True)
    thread.start()
    logger.info("Channel Gateway 已在后台启动（与 TUI 共享会话）")
    return gateway, thread
