from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from agent.channels.base import Channel, ChannelMessage, ChannelSpec
from agent.channels.router import ChannelRouter
from agent.channels.utils import split_text
from agent.config import WeChatOfficialConfig

if TYPE_CHECKING:
    from wechatpy import WeChatClient
    from wechatpy.replies import BaseReply

logger = logging.getLogger(__name__)


class WeChatOfficialChannel(Channel):
    """微信公众号（服务号/订阅号）Webhook Channel。"""

    def __init__(self, settings: WeChatOfficialConfig, router: ChannelRouter) -> None:
        self.settings = settings
        self.router = router
        self._client: WeChatClient | None = None
        self._crypto = None
        self._lock = threading.Lock()

    @property
    def spec(self) -> ChannelSpec:
        return ChannelSpec(
            id="wechat",
            title="微信公众号",
            description="通过公众号服务器配置 URL 接收用户消息，并用客服消息接口回复。",
        )

    def start(self) -> None:
        from wechatpy import WeChatClient

        if not self.settings.app_id or not self.settings.app_secret:
            raise ValueError("微信公众号未配置 WECHAT_APP_ID / WECHAT_APP_SECRET")

        self._client = WeChatClient(self.settings.app_id, self.settings.app_secret)

        if self.settings.encoding_aes_key:
            from wechatpy.crypto import WeChatCrypto

            self._crypto = WeChatCrypto(
                self.settings.token,
                self.settings.encoding_aes_key,
                self.settings.app_id,
            )

        logger.info(
            "微信公众号 channel 已就绪，Webhook 路径: %s",
            self.settings.webhook_path,
        )

    def stop(self) -> None:
        self._client = None
        self._crypto = None

    def can_handle(self, method: str, path: str) -> bool:
        return path.rstrip("/") == self.settings.webhook_path.rstrip("/")

    def handle_http(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET":
            return self._handle_verify(query)
        if method == "POST":
            return self._handle_message(query, body)
        return 405, {"Content-Type": "text/plain; charset=utf-8"}, b"Method Not Allowed"

    def _handle_verify(
        self, query: dict[str, list[str]]
    ) -> tuple[int, dict[str, str], bytes]:
        from wechatpy.exceptions import InvalidSignatureException
        from wechatpy.utils import check_signature

        signature = self._q(query, "signature")
        timestamp = self._q(query, "timestamp")
        nonce = self._q(query, "nonce")
        echostr = self._q(query, "echostr")

        try:
            if self._crypto:
                echostr = self._crypto.check_signature(signature, timestamp, nonce, echostr)
                return 200, {"Content-Type": "text/plain; charset=utf-8"}, echostr.encode("utf-8")
            check_signature(self.settings.token, signature, timestamp, nonce)
            return 200, {"Content-Type": "text/plain; charset=utf-8"}, echostr.encode("utf-8")
        except InvalidSignatureException:
            logger.warning("微信公众号签名校验失败")
            return 403, {"Content-Type": "text/plain; charset=utf-8"}, b"invalid signature"

    def _handle_message(
        self, query: dict[str, list[str]], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        from wechatpy import parse_message
        from wechatpy.exceptions import InvalidSignatureException
        from wechatpy.utils import check_signature

        signature = self._q(query, "signature")
        timestamp = self._q(query, "timestamp")
        nonce = self._q(query, "nonce")

        try:
            if self._crypto:
                msg_xml = self._crypto.decrypt_message(
                    body.decode("utf-8"),
                    signature,
                    timestamp,
                    nonce,
                )
            else:
                check_signature(self.settings.token, signature, timestamp, nonce)
                msg_xml = body.decode("utf-8")
        except InvalidSignatureException:
            logger.warning("微信公众号消息签名校验失败")
            return 403, {"Content-Type": "text/plain; charset=utf-8"}, b"invalid signature"
        except Exception as exc:
            logger.exception("微信公众号消息解密失败")
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, str(exc).encode("utf-8")

        msg = parse_message(msg_xml)
        if msg.type == "event" and getattr(msg, "event", "") == "subscribe":
            welcome = (
                f"你好，我是 {self.router.config.agent_name}。"
                "直接发送文字即可对话，支持 /reset、/memory、/role 等命令。"
            )
            return self._passive_reply_text(msg, welcome)

        if msg.type != "text" or not getattr(msg, "content", "").strip():
            return self._passive_reply_text(msg, "当前仅支持文字消息。")

        openid = msg.source
        if not self._is_allowed(openid):
            logger.info("拒绝未授权用户: %s", openid)
            return self._passive_reply_text(msg, "你暂无使用权限，请联系管理员添加 OpenID。")

        user_text = msg.content.strip()
        if user_text in {"/ping", "ping"}:
            return self._passive_reply_text(msg, "pong")

        # LLM 响应较慢，先被动回复提示，再异步客服消息推送完整答案
        threading.Thread(
            target=self._process_async,
            args=(openid, user_text, msg.id),
            daemon=True,
        ).start()
        return self._passive_reply_text(msg, "已收到，正在思考…")

    def _process_async(self, openid: str, text: str, message_id: str) -> None:
        message = ChannelMessage(
            channel_id="wechat",
            peer_id=openid,
            text=text,
            message_id=str(message_id or ""),
            account_id="official",
        )
        try:
            reply = self.router.handle(message)
        except Exception as exc:
            logger.exception("处理微信消息失败")
            reply_text = f"处理失败: {exc}"
        else:
            reply_text = reply.text

        self._send_text_chunks(openid, reply_text)

    def _send_text_chunks(self, openid: str, text: str) -> None:
        client = self._client
        if client is None:
            logger.error("微信客户端未初始化，无法发送消息")
            return

        chunks = split_text(text)
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"[{index}/{len(chunks)}] " if len(chunks) > 1 else ""
            try:
                client.message.send_text(openid, f"{prefix}{chunk}")
            except Exception as exc:
                logger.exception("发送微信客服消息失败: %s", exc)
                break

    def _passive_reply_text(self, msg, text: str) -> tuple[int, dict[str, str], bytes]:
        from wechatpy.replies import TextReply

        reply = TextReply(content=text, message=msg)
        payload = reply.render()
        if self._crypto:
            timestamp = str(int(__import__("time").time()))
            nonce = __import__("secrets").token_hex(8)
            payload = self._crypto.encrypt_message(payload, nonce, timestamp)
        return 200, {"Content-Type": "application/xml; charset=utf-8"}, payload.encode("utf-8")

    def _is_allowed(self, openid: str) -> bool:
        if not self.settings.allowed_openids:
            return True
        return openid in self.settings.allowed_openids

    @staticmethod
    def _q(query: dict[str, list[str]], key: str) -> str:
        values = query.get(key) or [""]
        return values[0] if values else ""


def parse_query(raw_query: str) -> dict[str, list[str]]:
    return parse_qs(raw_query, keep_blank_values=True)
